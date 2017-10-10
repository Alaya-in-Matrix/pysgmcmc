from abc import ABCMeta, abstractmethod


class StepsizeSchedule(object):
    """ Generic base class for all stepsize schedules. """
    __metaclass__ = ABCMeta

    @abstractmethod
    def __next__(self):
        """ Compute and return the next stepsize according to this schedule.

        Returns
        ----------
        next_stepsize : float
            Next stepsize to use according to this schedule.
        """
        raise NotImplementedError()

    def __iter__(self):
        return self

    @abstractmethod
    def update(self, *args, **kwargs):
        """ Update this schedule with new information. What information
            will be relevant depends on the type of schedule used.
            Information may e.g. include cost values for the last step size
            used, effective sample sizes of a sampler, values of other
            hyperparameters etc.

        """
        raise NotImplementedError()


class ConstantStepsizeSchedule(StepsizeSchedule):
    """ Trivial schedule that keeps the stepsize at a constant value.  """
    def __init__(self, constant_value):
        self.initial_value = constant_value

    def __next__(self):
        """ Calling `next(schedule)` always returns the schedules initial value,
            which is never changed.

        Returns
        ----------
        constant_value : float
            Constant value associated with this `ConstantStepsizeSchedule`
            object.

        Examples
        ----------
        Proof of concept:

        >>> schedule = ConstantStepsizeSchedule(0.01)
        >>> schedule.initial_value
        0.01
        >>> next(schedule)
        0.01
        >>> from itertools import islice
        >>> list(islice(schedule, 4))
        [0.01, 0.01, 0.01, 0.01]

        """
        return self.initial_value

    def update(self, *args, **kwargs):
        """ Updating a constant stepsize schedule is a no-op. """
        pass
