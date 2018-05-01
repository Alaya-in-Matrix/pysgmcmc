# vim:foldmethod=marker
import logging
import typing
from itertools import islice

import numpy as np
import torch
import torch.nn as nn
from torch.utils import data as data_utils
from torch.nn.modules.loss import _Loss, _assert_no_grad
from tqdm import tqdm

from pysgmcmc.data.utils import InfiniteDataLoader

#  Loss {{{ #
class NegativeLogLikelihood(_Loss):
    def __init__(self, parameters, num_datapoints, size_average=False, reduce=True):
        assert (not size_average) and reduce
        super().__init__(size_average, reduce)
        self.parameters = tuple(parameters)
        self.num_datapoints = num_datapoints

    def forward(self, input, target):
        _assert_no_grad(target)

        batch_size, *_ = target.shape
        prediction_mean = input[:, 0].view(-1, 1)

        log_prediction_variance = input[:, 1].view(-1, 1)
        prediction_variance_inverse = 1. / (torch.exp(log_prediction_variance) + 1e-16)

        mean_squared_error = (target - prediction_mean) ** 2

        log_likelihood = torch.sum(
            torch.sum(
                -mean_squared_error * 0.5 * prediction_variance_inverse -
                0.5 * log_prediction_variance,
                dim=1
            )
        )

        log_likelihood /= batch_size

        log_likelihood += (
            log_variance_prior(log_prediction_variance) / self.num_datapoints
        )

        log_likelihood += weight_prior(self.parameters) / self.num_datapoints

        return -log_likelihood
#  }}} Loss #


#  Helpers {{{ #

def default_network(input_dimensionality: int):
    class AppendLayer(nn.Module):
        def __init__(self, bias=True):
            super().__init__()
            if bias:
                self.bias = nn.Parameter(torch.Tensor(1, 1))
            else:
                self.register_parameter('bias', None)

        def forward(self, x):
            return torch.cat((x, self.bias * torch.ones_like(x)), dim=1)

    def init_weights(module):
        if type(module) == AppendLayer:
            nn.init.constant_(module.bias, val=np.log(1e-3))
        elif type(module) == nn.Linear:
            # Mode must be `fan_out` (rather than `fan_in` as e.g. in keras),
            # see pysgmcmc/tests/models/test_weight_initializers.py
            nn.init.kaiming_normal_(
                module.weight, mode="fan_out", nonlinearity="linear"
            )
            nn.init.constant_(module.bias, val=0.0)

    return nn.Sequential(
        nn.Linear(input_dimensionality, 50),
        nn.Tanh(),
        nn.Linear(50, 50),
        nn.Tanh(),
        nn.Linear(50, 50),
        nn.Tanh(),
        nn.Linear(50, 1),
        AppendLayer()
    ).apply(init_weights)


def safe_division(x, y, small_constant=1e-16):
    """ Computes `x / y` after adding a small appropriately signed constant to `y`.
        Adding a small constant avoids division-by-zero artefacts that may
        occur due to precision errors.

    Parameters
    ----------
    x: np.ndarray
        Left-side operand of division.
    y: np.ndarray
        Right-side operand of division.
    small_constant: float, optional
        Small constant to add to/subtract from `y` before computing `x / y`.
        Defaults to `1e-16`.

    Returns
    ----------
    division_result : np.ndarray
        Result of `x / y` after adding a small appropriately signed constant
        to `y` to avoid division by zero.

    Examples
    ----------

    Will safely avoid divisions-by-zero under normal circumstances:

    >>> import numpy as np
    >>> x = np.asarray([1.0])
    >>> inf_tensor = x / 0.0  # will produce "inf" due to division-by-zero
    >>> bool(np.isinf(inf_tensor))
    True
    >>> z = safe_division(x, 0., small_constant=1e-16)  # will avoid division-by-zero
    >>> bool(np.isinf(z))
    False

    To see that simply adding a positive constant may fail, consider the
    following example. Note that this function handles such corner cases correctly:

    >>> import numpy as np
    >>> x, y = np.asarray([1.0]), np.asarray([-1e-16])
    >>> small_constant = 1e-16
    >>> inf_tensor = x / (y + small_constant)  # simply adding constant exhibits division-by-zero
    >>> bool(np.isinf(inf_tensor))
    True
    >>> z = safe_division(x, y, small_constant=1e-16)  # will avoid division-by-zero
    >>> bool(np.isinf(z))
    False

    """
    if (np.asarray(y) == 0).all():
        return np.true_divide(x, small_constant)
    return np.true_divide(x, np.sign(y) * small_constant + y)


def zero_mean_unit_var_normalization(X, mean=None, std=None):
    if mean is None:
        mean = np.mean(X, axis=0)
    if std is None:
        std = np.std(X, axis=0)

    X_normalized = safe_division(X - mean, std)

    return X_normalized, mean, std


def zero_mean_unit_var_unnormalization(X_normalized, mean, std):
    return X_normalized * std + mean
#  }}} Helpers #


#  Loss {{{ #

#  Prios {{{ #

def log_variance_prior(log_variance,
                       mean: float=1e-6,
                       variance: float=0.01):
    return torch.mean(
        torch.sum(
            ((-(log_variance - torch.log(torch.Tensor([mean]))) ** 2) /
             ((2. * variance))) - 0.5 * torch.log(torch.Tensor([variance])),
            dim=1
        )
    )


def weight_prior(parameters,
                 wdecay: float=1.):
    log_likelihood = 0.
    num_parameters = 0

    for parameter in parameters:
        log_likelihood += torch.sum(-wdecay * 0.5 * (parameter ** 2))
        num_parameters += np.prod(parameter.shape)

    return log_likelihood / (num_parameters + 1e-16)
#  }}} Prios #




class BayesianNeuralNetwork(object):
    def __init__(self, network_architecture=default_network,
                 normalize_input=True, normalize_output=True,
                 loss=NegativeLogLikelihood,
                 metrics=(nn.MSELoss(size_average=False),),
                 num_steps=50000, burn_in_steps=3000,
                 keep_every=100, num_nets=100, batch_size=20,
                 progress=True,
                 optimizer=torch.optim.SGD, **optimizer_kwargs) -> None:

        assert num_steps > burn_in_steps
        self.burn_in_steps = burn_in_steps
        self.num_steps = num_steps - self.burn_in_steps

        assert batch_size > 0
        self.batch_size = batch_size

        assert keep_every > 0
        self.keep_every = keep_every

        assert num_nets > 0
        self.num_nets = num_nets

        self.num_steps = min(
            self.num_steps, self.keep_every * self.num_nets
        )

        self.num_iterations = self.num_steps + self.burn_in_steps
        logging.info(
            "Performing '{}' iterations in total.".format(self.num_iterations)
        )

        assert isinstance(normalize_input, bool)
        self.normalize_input = normalize_input

        assert isinstance(normalize_output, bool)
        self.normalize_output = normalize_output

        self.network_architecture = network_architecture

        self.optimizer = optimizer
        self.loss_function = loss

        self.metric_functions = list(metrics)

        self.sampled_weights = []  # type: typing.List[typing.Tuple[typing.Any, ...]]

        self.optimizer_kwargs = optimizer_kwargs

        self.progress = progress

    @property
    def network_weights(self):
        return self.model.parameters()

    @network_weights.setter
    def network_weights(self, weights):
        for parameter, sample in zip(self.model.parameters(), weights):
            with torch.no_grad():
                parameter.copy_(torch.from_numpy(sample))

    def _keep_sample(self, epoch: int) -> bool:
        if epoch < self.burn_in_steps:
            return False
        sample_step = epoch - self.burn_in_steps
        return (sample_step % self.keep_every) == 0

    def _log_progress(self, epoch: int) -> bool:
        return self.progress and (epoch % 100 == 0)

    def train(self, x_train: np.ndarray, y_train: np.ndarray):
        self.sampled_weights.clear()

        self.x_train, self.y_train = np.asarray(x_train), np.asarray(y_train)

        if self.normalize_input:
            self.x_train, self.x_mean, self.x_std = zero_mean_unit_var_normalization(
                self.x_train
            )

        if self.normalize_output:
            self.y_train, self.y_mean, self.y_std = zero_mean_unit_var_normalization(
                self.y_train
            )

        num_datapoints, input_dimensionality = self.x_train.shape

        self.model = self.network_architecture(
            input_dimensionality=input_dimensionality,
        )

        optimizer = self.optimizer(
            self.model.parameters(), **self.optimizer_kwargs
        )

        train_dataset = data_utils.TensorDataset(
            torch.Tensor(self.x_train), torch.Tensor(self.y_train)
        )

        train_loader = InfiniteDataLoader(
            dataset=train_dataset, batch_size=self.batch_size, shuffle=False
        )

        loss_function = self.loss_function(
            parameters=self.network_weights, num_datapoints=num_datapoints
        )

        if self.progress:
            progress_bar = tqdm(
                islice(enumerate(train_loader), self.num_iterations),
                total=self.num_iterations,
                bar_format="{n_fmt}/{total_fmt}[{bar}] - {remaining} - {postfix}"
            )
        else:
            progress_bar = islice(enumerate(train_loader), self.num_iterations)

        for epoch, (x_batch, y_batch) in progress_bar:
            batch_prediction = self.model(x_batch)
            # XXX: What does my loss function do that MSELoss does not?
            # => That must be the problem..
            # loss = torch.nn.MSELoss(size_average=False)(batch_prediction[:, 0], y_batch)
            # loss = torch.nn.MSELoss(size_average=False)(batch_prediction[:, 0], y_batch)
            loss = loss_function(batch_prediction, y_batch)

            optimizer.zero_grad()
            loss.backward()
            optimizer.step()

            #  Progress: print metrics {{{ #

            if self._log_progress(epoch):
                metric_values = [
                    metric_function(input=batch_prediction[:, 0], target=y_batch)
                    for metric_function in self.metric_functions
                ]

                def get_name(metric):
                    try:
                        name = metric.__name__
                    except AttributeError:
                        return metric.__class__.__name__
                    else:
                        if metric == NegativeLogLikelihood:
                            return "NLL"
                        return name

                metric_names = [
                    get_name(metric)
                    for metric in self.metric_functions + [self.loss_function]
                ]

                progress_bar.set_postfix_str(" - ".join([
                    "{name}: {value}".format(name=name, value=value.detach().numpy())
                    for name, value in zip(
                        metric_names, metric_values + [loss]
                    )
                ]))
            #  }}} Progress: print metrics #

            if self._keep_sample(epoch):
                sample = tuple(
                    np.asarray(torch.tensor(parameter.data).numpy())
                    for parameter in self.network_weights
                )
                self.sampled_weights.append(sample)


        self.is_trained = True

    #  Predict {{{ #
    def predict(self, x_test: np.ndarray, return_individual_predictions: bool=False):
        assert self.is_trained
        assert isinstance(return_individual_predictions, bool)

        x_test_ = np.array(x_test)
        if self.normalize_input:
            x_test_, _, _ = zero_mean_unit_var_normalization(
                x_test, self.x_mean, self.x_std
            )

        test_data = torch.from_numpy(x_test_).float()

        def network_predict(weights, test_data):
            self.network_weights = weights
            with torch.no_grad():
                return self.model(test_data).numpy()[:, 0]

        network_outputs = [
            network_predict(weights=sample, test_data=test_data)
            for sample in self.sampled_weights
        ]
        print(len(network_outputs))
        prediction_mean = np.mean(network_outputs, axis=0)

        prediction_variance = np.mean(
            (network_outputs - prediction_mean) ** 2, axis=0
        )

        if self.normalize_output:
            prediction_mean = zero_mean_unit_var_unnormalization(
                prediction_mean, self.y_mean, self.y_std
            )
            prediction_variance *= self.y_std ** 2
        print("VARIANCE:", prediction_variance)

        return prediction_mean, prediction_variance
    #  }}} Predict #
