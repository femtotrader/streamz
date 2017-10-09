from functools import partial
import operator
from time import time

from .utils import M
import numpy as np
import pandas as pd

from .sources import Source
from .collection import Streaming, _subtypes


class StreamingFrame(Streaming):
    def sum(self):
        return self.accumulate_partitions(_accumulate_sum, start=0)

    def round(self, decimals=0):
        return self.map_partitions(M.round, decimals=decimals)

    def rolling(self, window, min_periods=1):
        """ Compute rolling aggregations

        When followed by an aggregation method like ``sum``, ``mean``, or
        ``std`` this produces a new Streaming dataframe whose values are
        aggregated over that window.

        The window parameter can be either a number of rows or a timedelta like
        ``"2 minutes"` in which case the index should be a datetime index.

        This operates by keeping enough of a backlog of records to maintain an
        accurate stream.  It performs a copy at every added dataframe.  Because
        of this it may be slow if the rolling window is much larger than the
        average stream element.

        Parameters
        ----------
        window: int or timedelta
            Window over which to roll

        Returns
        -------
        Rolling object
        """
        return Rolling(self, window, min_periods)


class Rolling(object):
    def __init__(self, sdf, window, min_periods):
        self.sdf = sdf
        if not isinstance(window, int):
            window = pd.Timedelta(window)
            min_periods = 1
        self.window = window
        self.min_periods = min_periods

    def __getitem__(self, key):
        sdf = self.sdf[key]
        return Rolling(sdf, self.window, self.min_periods)

    def __getattr__(self, key):
        if key in self.sdf.columns or not len(self.sdf.columns):
            return self[key]
        else:
            raise AttributeError("StreamingSeriesGroupby has no attribute %r" % key)

    def _known_aggregation(self, op, *args, **kwargs):
        return self.sdf.accumulate_partitions(rolling_accumulator,
                                              window=self.window,
                                              op=op,
                                              args=args,
                                              kwargs=kwargs,
                                              start=(),
                                              returns_state=True)

    def sum(self):
        return self._known_aggregation('sum')

    def mean(self):
        return self._known_aggregation('mean')

    def min(self):
        return self._known_aggregation('min')

    def max(self):
        return self._known_aggregation('max')

    def median(self):
        return self._known_aggregation('median')

    def std(self, *args, **kwargs):
        return self._known_aggregation('std', *args, **kwargs)

    def var(self, *args, **kwargs):
        return self._known_aggregation('var', *args, **kwargs)

    def count(self, *args, **kwargs):
        return self._known_aggregation('count', *args, **kwargs)

    def aggregate(self, *args, **kwargs):
        return self._known_aggregation('aggregate', *args, **kwargs)

    def quantile(self, *args, **kwargs):
        return self._known_aggregation('quantile', *args, **kwargs)


def rolling_accumulator(acc, new, window=None, op=None, args=(), kwargs={}):
    if len(acc):
        df = pd.concat([acc, new])
    else:
        df = new
    result = getattr(df.rolling(window), op)(*args, **kwargs)
    if isinstance(window, int):
        new_acc = df.iloc[-window:]
    else:
        new_acc = df.loc[result.index.max() - window:]
    result = result.iloc[len(acc):]
    return new_acc, result


class StreamingDataFrame(StreamingFrame):
    """ A Streaming dataframe

    This is a logical collection over a stream of Pandas dataframes.
    Operations on this object will translate to the appropriate operations on
    the underlying Pandas dataframes.

    See Also
    --------
    streams.dataframe.StreamingSeries
    streams.sequence.StreamingSequence
    """
    _subtype = pd.DataFrame

    @property
    def columns(self):
        return self.example.columns

    def __getitem__(self, index):
        return self.map_partitions(operator.getitem, index)

    def __getattr__(self, key):
        if key in self.columns or not len(self.columns):
            return self.map_partitions(getattr, key)
        else:
            raise AttributeError("StreamingDataFrame has no attribute %r" % key)

    def __dir__(self):
        o = set(dir(type(self)))
        o.update(self.__dict__)
        o.update(c for c in self.columns if
                 (isinstance(c, pd.compat.string_types) and
                 pd.compat.isidentifier(c)))
        return list(o)

    def verify(self, x):
        super(StreamingDataFrame, self).verify(x)
        if list(x.columns) != list(self.example.columns):
            raise IndexError("Input expected to have columns %s, got %s" %
                             (self.example.columns, x.columns))

    def mean(self):
        start = pd.DataFrame({'sums': 0, 'counts': 0},
                             index=self.example.columns)
        return self.accumulate_partitions(_accumulate_mean, start=start,
                                          returns_state=True)

    def groupby(self, other):
        return StreamingSeriesGroupby(self, other)

    def assign(self, **kwargs):
        def concat(tup, columns=None):
            result = pd.concat(tup, axis=1)
            result.columns = columns
            return result
        columns, values = zip(*kwargs.items())
        stream = self.stream.zip(*[v.stream for v in values])
        stream = stream.map(concat, columns=list(self.columns) + list(columns))
        example = self.example.assign(**{c: v.example for c, v in kwargs.items()})
        return StreamingDataFrame(stream, example)

    def to_frame(self):
        return self

    def __setitem__(self, key, value):
        if isinstance(value, StreamingSeries):
            result = self.assign(**{key: value})
        elif isinstance(value, StreamingDataFrame):
            result = self.assign(**{k: value[c] for k, c in zip(key, value.columns)})
        else:
            example = self.example.copy()
            example[key] = value
            result = self.map_partitions(pd.DataFrame.assign, **{key: value})

        self.stream = result.stream
        self.example = result.example
        return self


class StreamingSeries(StreamingFrame):
    """ A Streaming series

    This is a logical collection over a stream of Pandas series objects.
    Operations on this object will translate to the appropriate operations on
    the underlying Pandas series.

    See Also
    --------
    streams.dataframe.StreamingDataFrame
    streams.sequence.StreamingSequence
    """
    _subtype = pd.Series

    def mean(self):
        start = pd.Series({'sums': 0, 'counts': 0})
        return self.accumulate_partitions(_accumulate_mean, start=start,
                                          returns_state=True)

    def to_frame(self):
        return self.map_partitions(M.to_frame)


def _accumulate_mean(accumulator, new):
    accumulator = accumulator.copy()
    accumulator['sums'] += new.sum()
    accumulator['counts'] += new.count()
    result = accumulator['sums'] / accumulator['counts']
    return accumulator, result


def _accumulate_sum(accumulator, new):
    return accumulator + new.sum()


def _accumulate_size(accumulator, new):
    return accumulator + new.size()


class StreamingSeriesGroupby(object):
    def __init__(self, root, grouper, index=None):
        self.root = root
        self.grouper = grouper
        self.index = index

    def __getitem__(self, index):
        return StreamingSeriesGroupby(self.root, self.grouper, index)

    def __getattr__(self, key):
        if key in self.root.columns or not len(self.root.columns):
            return self[key]
        else:
            raise AttributeError("StreamingSeriesGroupby has no attribute %r" % key)

    def sum(self):
        func = _accumulate_groupby_sum
        start = 0
        if isinstance(self.grouper, Streaming):
            func = partial(func, index=self.index)
            example = self.root.example.groupby(self.grouper.example)
            if self.index is not None:
                example = example[self.index]
            example = example.sum()
            stream = self.root.stream.zip(self.grouper.stream)
            stream = stream.accumulate(func, start=start)
        else:
            func = partial(func, grouper=self.grouper, index=self.index)
            example = self.root.example.groupby(self.grouper)
            if self.index is not None:
                example = example[self.index]
            example = example.sum()
            stream = self.root.stream.accumulate(func, start=start)
        if isinstance(example, pd.DataFrame):
            return StreamingDataFrame(stream, example)
        else:
            return StreamingSeries(stream, example)

    def mean(self):
        # TODO, there is a lot of copy-paste with the code above
        # TODO, we should probably define groupby.aggregate
        func = _accumulate_groupby_mean
        start = (0, 0)
        if isinstance(self.grouper, Streaming):
            func = partial(func, index=self.index)
            example = self.root.example.groupby(self.grouper.example)
            if self.index is not None:
                example = example[self.index]
            example = example.mean()
            stream = self.root.stream.zip(self.grouper.stream)
            stream = stream.accumulate(func, start=start, returns_state=True)
        else:
            func = partial(func, grouper=self.grouper, index=self.index)
            example = self.root.example.groupby(self.grouper)
            if self.index is not None:
                example = example[self.index]
            example = example.mean()
            stream = self.root.stream.accumulate(func, start=start,
                                                 returns_state=True)
        if isinstance(example, pd.DataFrame):
            return StreamingDataFrame(stream, example)
        else:
            return StreamingSeries(stream, example)


def _accumulate_groupby_sum(accumulator, new, grouper=None, index=None):
    if isinstance(new, tuple):  # zipped
        assert grouper is None
        new, grouper = new
    g = new.groupby(grouper)
    if index is not None:
        g = g[index]
    if isinstance(accumulator, int):
        return g.sum()
    else:
        return accumulator.add(g.sum(), fill_value=0)


def _accumulate_groupby_mean(accumulator, new, grouper=None, index=None):
    if isinstance(new, tuple):  # zipped
        assert grouper is None
        new, grouper = new
    g = new.groupby(grouper)
    if index is not None:
        g = g[index]

    (sums, counts) = accumulator
    if isinstance(sums, int):  # first time
        sums = g.sum()
        counts = g.count()
    else:
        sums = sums.add(g.sum(), fill_value=0)
        counts = counts.add(g.count(), fill_value=0)
    return (sums, counts), sums / counts


_subtypes.append((pd.DataFrame, StreamingDataFrame))
_subtypes.append((pd.Series, StreamingSeries))


class Random(StreamingDataFrame):
    """ A streaming dataframe of random data

    The x column is uniformly distributed.
    The y column is poisson distributed.
    The z column is normally distributed.

    Parameters
    ----------
    freq: timedelta
        The time interval between records
    interval: timedelta
        The time interval between new dataframes, should be significantly
        larger than freq

    Example
    -------
    >>> source = Random(freq='100ms', interval='1s')  # doctest: +SKIP
    """
    def __init__(self, freq='100ms', interval='500ms'):
        from tornado.ioloop import PeriodicCallback
        self.last = time()
        self.freq = pd.Timedelta(freq)
        self.interval = pd.Timedelta(interval).total_seconds() * 1000

        self.pc = PeriodicCallback(self._trigger, self.interval)
        self.pc.start()

        super(Random, self).__init__(Source(), self._next_df())

    def _next_df(self):
        now = time()
        index = pd.DatetimeIndex(start=(self.last + self.freq.total_seconds()) * 1e9,
                                 end=time() * 1e9,
                                 freq=self.freq)

        df = pd.DataFrame({'x': np.random.random(len(index)),
                           'y': np.random.poisson(size=len(index)),
                           'z': np.random.normal(0, 1, size=len(index))},
                           index=index)
        self.last = now
        return df

    def _trigger(self):
        self.emit(self._next_df())