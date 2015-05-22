# encoding: utf-8

"""
"""

from copy import deepcopy
import hashlib
import json
import yaml
import csv
import prettytable
import collections
import time
from functools import partial

import addressable
import inspector

from .columns import Column, Segment
from . import utils
from . import errors


class Report(object):
    """
    Executing a query will return a report, which contains the requested data.

    Queries are executed and turned into a report lazily, whenever data is requested.
    You can also explicitly generate a report from a query by using the `Query#get` method.

    ```python
    # will return a query object
    profile.core.query.metrics('pageviews').range('yesterday')
    # will return a report object
    profile.core.query.metrics('pageviews').range('yesterday').get()
    # will generate a report object and return its rows -- these
    # two are equivalent
    profile.core.query.metrics('pageviews').range('yesterday').rows
    profile.core.query.metrics('pageviews').range('yesterday').get().rows
    ```

    You can access the data in a Report object both rowwise and columnwise.

    ```python
    report = query.metrics('pageviews', 'sessions').range('yesterday')
    # first ten rows
    report.rows[:10]
    # work with just session data points
    report['sessions'][:10]
    report.rows[:10]['sessions']
    ```

    For simple data structures, there are also some shortcuts.

    These shortcuts are available both directly on Report objects
    and lazily-loaded via Query objects.

    ```python
    # reports with a single value
    query = profile.core.query('pageviews').range('yesterday')
    report = query.get()
    assert query.value == report.value
    # reports with a single metric
    profile.core.query('pageviews').daily('yesterday', days=-10).values
    # reports with a single result
    query = profile.core.query(['pageviews', 'sessions']).range('yesterday')
    assert query.first == query.last
    ```
    """

    def __init__(self, raw, query):
        self.raw = []
        self.queries = []

        registry = query.api.all_columns
        headers = [registry[header['name']] for header in raw['columnHeaders']]
        self.slugs = [header.pyslug for header in headers]
        self.names = [header.name for header in headers]
        self.row_cls = collections.namedtuple('Row', self.slugs)
        self.headers = addressable.List(headers,
            indices=registry.indexed_on, insensitive=True)
        self.metrics = set()
        self.dimensions = set()
        self.rows = []
        self.append(raw, query)

    def append(self, raw, query):
        self.raw.append(raw)
        self.queries.append(query)
        self.metrics.update(query.raw['metrics'])
        self.dimensions.update(query.raw.get('dimensions', []))
        self.is_complete = not 'nextLink' in raw

        casters = [column.cast for column in self.headers]

        # if no rows were returned, the GA API doesn't
        # include the `rows` key at all
        for row in self.raw[-1].get('rows', []):
            typed_row = [casters[i](row[i]) for i in range(len(self.headers))]
            typed_tuple = self.row_cls(*typed_row)
            self.rows.append(typed_tuple)

        # TODO: figure out how this works with paginated queries
        self.totals = raw['totalsForAllResults']
        # more intuitive when querying for just a single metric
        self.total = list(raw['totalsForAllResults'].values())[0]
        # print(self.totals)

    @property
    def first(self):
        if len(self.rows) == 0:
            return None
        else:
            return self.rows[0]

    @property
    def last(self):
        if len(self.rows) == 0:
            return None
        else:
            return self.rows[-1]

    @property
    def value(self):
        if len(self.rows) == 0:
            return None
        elif len(self.rows) == 1:
            return self.values[0]
        else:
            raise ValueError("This report contains multiple rows or metrics. Please use `rows`, `first`, `last` or a column name.")

    @property
    def values(self):
        if len(self.metrics) == 1:
            raw_metric = list(self.metrics).pop()
            metric = self.headers[raw_metric]
            return self[metric]
        else:
            raise ValueError("This report contains multiple metrics. Please use `rows`, `first`, `last` or a column name.")

    def serialize(self, format=None, with_metadata=False):
        if not format:
            return self.as_dict(with_metadata=with_metadata)
        elif format == 'json':
            return json.dumps(self.as_dict(with_metadata=with_metadata), indent=4)
        elif format == 'csv':
            buf = utils.StringIO()
            writer = csv.writer(buf)
            writer.writerow(self.names)
            writer.writerows(self.rows)
            return buf.getvalue()
        elif format == 'ascii':
            table = prettytable.PrettyTable(self.names)
            table.align = 'l'
            for row in self.rows:
                table.add_row(row)
            if with_metadata:
                return utils.format("""
                    {title}
                    {table}
                    """, title=self.queries[0].title, table=table)
            else:
                return table

    def as_dict(self, with_metadata=False):
        serialized = []
        for row in self.rows:
            row = row._asdict()
            for key, value in row.items():
                row[key] = utils.date.serialize(value)
            serialized.append(row)

        if with_metadata:
            return {
                'title': self.queries[0].title,
                'queries': self.queries,
                'metrics': self.metrics,
                'dimensions': self.dimensions,
                'results': serialized,
                }
        else:
            return serialized

    def as_dataframe(self):
        import pandas
        # passing every row as a dictionary is not terribly efficient,
        # but it works for now
        return pandas.DataFrame(self.as_dict())

    def __getitem__(self, key):
        try:
            if isinstance(key, Column):
                key = key.slug
            i = self.headers.index(key)
            return [row[i] for row in self.rows]
        except ValueError:
            raise ValueError(key + " not in column headers")

    def __iter__(self):
        raise NotImplementedError()

    def __len__(self):
        return len(self.rows)

    # TODO: would be cool if we could split up headers
    # into metrics vs. dimensions so we could say
    # "pageviews by day, browser"
    # (also see `title` and `description` on query objects)
    def __repr__(self):
        headers = [header.name for header in self.headers]
        return '<googleanalytics.query.Report object: {}'.format(', '.join(headers))


def select(source, selection):
    selections = []
    for key, values in selection.items():
        if '__' in key:
            column, method = key.split('__')
        else:
            column = key
            method = 'eq'

        if not hasattr(Column, method):
            raise ValueError("{method} is not a valid selector. Choose from: {options}".format(
                method=method,
                options=', '.join(Column.selectors),
                ))

        column = source[column]
        selector = getattr(column, method)

        if not isinstance(values, (list, tuple)):
            values = [values]

        # e.g. source=['cpc', 'cpm'] will return an OR
        # filter for these two sources
        for value in values:
            selections.append(selector(value))

    return selections


class Query(object):
    """
    Return a query for certain metrics and dimensions.

    ```python
    # pageviews (metric) as a function of geographical region
    profile.core.query('pageviews', 'region')
    # pageviews as a function of browser
    profile.core.query(['pageviews'], ['browser'])
    ```

    The returned query can then be further refined using
    all methods available on the `CoreQuery` object, such as
    `limit`, `sort`, `segment` and so on.

    Metrics and dimensions may be either strings (the column id or
    the human-readable column name) or Metric or Dimension
    objects.

    Metrics and dimensions specified as a string are not case-sensitive.

    ```python
    profile.query('PAGEVIEWS')
    ```

    If specifying only a single metric or dimension, you can
    but are not required to wrap it in a list.
    """

    _lock = 0

    def __init__(self, api, metrics=[], dimensions=[], meta=None, title=None):
        self._title = title
        self.raw = {'ids': 'ga:' + api.profile.id}
        self.meta = {}
        self.meta.update(meta or {})
        self.api = api
        self.profile = api.profile
        self.webproperty = api.profile.webproperty
        self.account = api.profile.webproperty.account
        self._report = None
        self._specify(metrics=metrics, dimensions=dimensions)

    # no not execute more than one query per second
    def _wait(self):
        now = time.time()
        elapsed = now - self._lock
        wait = max(0, 1 - elapsed)
        time.sleep(wait)
        self._lock = time.time()
        return wait

    @property
    def endpoint(self):
        return self.account.service.data().ga()

    def clone(self):
        query = self.__class__(api=self.api, meta=self.meta)
        query.raw = deepcopy(self.raw)
        query._report = None
        return query

    @utils.immutable
    def set(self, key=None, value=None, **kwargs):
        """
        `set` is a way to add raw properties to the request,
        for features that this module does not
        support or supports incompletely. For convenience's
        sake, it will serialize Column objects but will
        leave any other kind of value alone.
        """

        serialize = partial(self.api.columns.serialize, greedy=False)

        if key and value:
            self.raw[key] = serialize(value)
        elif key or kwargs:
            properties = key or kwargs
            for key, value in properties.items():
                self.raw[key] = serialize(value)
        else:
            raise ValueError(
                "Query#set requires a key and value, a properties dictionary or keyword arguments.")

        return self

    def _specify(self, metrics=[], dimensions=[]):
        serialize = partial(self.api.columns.serialize, wrap=True)

        metrics = serialize(metrics)
        dimensions = serialize(dimensions)
        self.raw.setdefault('metrics', []).extend(metrics)
        self.raw.setdefault('dimensions', []).extend(dimensions)

        return self

    # TODO: maybe do something smarter, like {granularity} {metrics}
    # by {dimensions} for {segment}, filtered by {filters}.
    # First {limit} results from {start} to {end} /
    # for {start=end}, sorted by {direction} {sort}.
    @property
    def description(self):
        """
        A list of the metrics this query will ask for.
        """

        if len(self.raw['metrics']):
            metrics = self.raw['metrics']
            head = metrics[0:-1] or metrics[0:1]
            text = ", ".join(head)
            if len(metrics) > 1:
                tail = metrics[-1]
                text = text + " and " + tail
        else:
            text = 'n/a'

        return text

    @property
    def title(self):
        return self._title or self.description

    @title.setter
    def title(self, value):
        self._title = value

    @inspector.implements(_specify)
    @utils.immutable
    def query(self, *vargs, **kwargs):
        """
        Return a new query with additional metrics and dimensions.
        If specifying only a single metric or dimension, you can
        but are not required to wrap it in a list.

        This interface is identical to the one you use to construct
        new queries, `Profile#query`. Look there for more details.
        """
        return self._specify(*vargs, **kwargs)

    @utils.immutable
    def metrics(self, *metrics):
        """
        Return a new query with additional metrics.

        ```python
        query.metrics('pageviews', 'page load time')
        ```
        """
        return self._specify(metrics=metrics)

    @utils.immutable
    def dimensions(self, *dimensions):
        """
        Return a new query with additional dimensions.

        ```python
        query.dimensions('search term', 'search depth')
        ```
        """
        return self._specify(dimensions=dimensions)

    @utils.immutable
    def sort(self, *columns, **options):
        """
        Return a new query which will produce results sorted by
        one or more metrics or dimensions. You may use plain
        strings for the columns, or actual `Column`, `Metric`
        and `Dimension` objects.

        Add a minus in front of the metric (either the string or
        the object) to sort in descending order.

        ```python
        # sort using strings
        query.sort('pageviews', '-device type')
        # alternatively, ask for a descending sort in a keyword argument
        query.sort('pageviews', descending=True)

        # sort using metric, dimension or column objects
        pageviews = profile.core.metrics['pageviews']
        query.sort(-pageviews)
        ```
        """

        sorts = self.meta.setdefault('sort', [])

        for column in columns:
            if isinstance(column, Column):
                identifier = column.id
            elif isinstance(column, utils.basestring):
                descending = column.startswith('-') or options.get('descending', False)
                identifier = self.api.columns[column.lstrip('-')].id
            else:
                raise ValueError("Can only sort on columns or column strings. Received: {}".format(column))

            if descending:
                sign = '-'
            else:
                sign = ''

            sorts.append(sign + identifier)

        self.raw['sort'] = ",".join(sorts)
        return self

    @utils.immutable
    def filter(self, value=None, **selection):
        """ Most of the actual functionality lives on the Column
        object and the `all` and `any` functions. """
        filters = self.meta.setdefault('filters', [])

        if value and len(selection):
            raise ValueError("Cannot specify a filter string and a filter keyword selection at the same time.")
        elif value:
            value = [value]
        elif len(selection):
            value = select(self.api.columns, selection)

        filters.append(value)
        self.raw['filters'] = utils.paste(filters, ',', ';')
        return self

    def build(self, copy=True):
        if copy:
            raw = deepcopy(self.raw)
        else:
            raw = self.raw

        raw['metrics'] = ','.join(self.raw['metrics'])
        
        if len(raw['dimensions']):
            raw['dimensions'] = ','.join(self.raw['dimensions'])
        else:
            raw['dimensions'] = None

        return raw

    @property
    def cacheable(self):
        start = 'start_date' in self.raw and not utils.date.is_relative(self.raw['start_date'])
        end = 'end_date' in self.raw and not utils.date.is_relative(self.raw['end_date'])
        return start and end

    @property
    def signature(self):
        query = self.build(copy=False)
        standardized_query = sorted(query.items(), key=lambda t: t[0])
        serialized_query = json.dumps(standardized_query)
        return hashlib.sha1(serialized_query.encode('utf-8')).hexdigest()

    def execute(self):
        raw = self.build()

        if self.api.cache and self.cacheable and self.api.cache.exists(self.signature):
            response = self.api.cache.get(raw)
        else:
            try:
                self._wait()
                response = self.endpoint.get(**raw).execute()
            except Exception as err:
                if isinstance(err, TypeError):
                    width = max(map(len, self.raw.keys()))
                    raw = [(key.ljust(width), value) for key, value in self.raw.items()]
                    parameters = utils.paste(raw, '\t', '\n')
                    diagnostics = utils.format(
                        """
                        {message}

                        The query you submitted was:

                        {parameters}
                        """, message=str(err), parameters=parameters)
                    raise errors.InvalidRequestError(diagnostics)
                elif hasattr(err, 'content'):
                    # circumvent bug in `googleapiclient` on Python 3
                    err.content = err.content.decode('utf-8')
                    raise errors.GoogleAnalyticsError(str(err))
                else:
                    raise err

        if self.api.cache and self.cacheable:
            self.api.cache.set(raw, response)

        return Report(response, self)

    @property
    def report(self):
        if not self._report:
            self._report = self.get()
        return self._report

    # lazy-loading shortcuts
    def __getattr__(self, name):
        if hasattr(self.report, name):
            return getattr(self.report, name)
        else:
            raise AttributeError("'{cls}' object and its associated 'Report' object have no attribute '{name}'".format(
                cls=self.__class__.__name__,
                name=name,
                ))

    def __repr__(self):
        return "<googleanalytics.query.{} object: {} ({})>".format(self.__class__.__name__, self.title, self.profile.name)


class CoreQuery(Query):
    """
    CoreQuery is the main way through which to produce reports
    from data in Google Analytics.

    The most important methods are:

    * `metrics` and `dimensions` (both of which you can also pass as
      lists when creating the query)
    * `range` and its shortcuts that have the granularity already set:
      `hourly`, `daily`, `weekly`, `monthly`, `yearly`
    * `filter` to filter which rows are analyzed before running the query
    * `segment` to filter down to a certain kind of session or user (as
      opposed to `filter` which works on individual rows of data)
    * `limit` to ask for a subset of results
    * `sort` to sort the query


    CoreQuery is mostly immutable: wherever possible, methods
    return a new query rather than modifying the existing one,
    so for example this works as you'd expect it to:

    ```python
    base = profile.query('pageviews')
    january = base.daily('2014-01-01', months=1).get()
    february = base.daily('2014-02-01', months=1).get()
    ```
    """

    # TODO (?)
    # fields
    # userIp / quotaUser
    # https://developers.google.com/analytics/devguides/reporting/core/v3/reference#q_summary

    PRECISION_LEVELS = ('FASTER', 'DEFAULT', 'HIGHER_PRECISION', )
    GRANULARITY_LEVELS = ('year', 'month', 'week', 'day', 'hour', )
    GRANULARITY_DIMENSIONS = (
        'ga:year', 'ga:yearMonth', 'ga:yearWeek',
        'ga:date', 'ga:dateHour',
    )

    @utils.immutable
    def precision(self, precision):
        """
        For queries that should run faster, you may specify a lower precision,
        and for those that need to be more precise, a higher precision:

        ```python
        # faster queries
        query.range('2014-01-01', '2014-01-31', precision=0)
        query.range('2014-01-01', '2014-01-31', precision='FASTER')
        # queries with the default level of precision (usually what you want)
        query.range('2014-01-01', '2014-01-31')
        query.range('2014-01-01', '2014-01-31', precision=1)
        query.range('2014-01-01', '2014-01-31', precision='DEFAULT')
        # queries that are more precise
        query.range('2014-01-01', '2014-01-31', precision=2)
        query.range('2014-01-01', '2014-01-31', precision='HIGHER_PRECISION')      
        ```
        """

        if isinstance(precision, int):
            precision = self.PRECISION_LEVELS[precision]

        if precision not in self.PRECISION_LEVELS:
            levels = ", ".join(self.PRECISION_LEVELS)
            raise ValueError("Precision should be one of: " + levels)

        if precision != 'DEFAULT':
            self.raw.update({'samplingLevel': precision})

        return self

    @utils.immutable
    def interval(self, granularity):
        """
        Note that if you don't specify a granularity (either through the `interval`
        method or through the `hourly`, `daily`, `weekly`, `monthly` or `yearly`
        shortcut methods) you will get only a single result, encompassing the
        entire date range, per metric.
        """

        if not isinstance(granularity, int):
            if granularity in self.GRANULARITY_LEVELS:
                granularity = self.GRANULARITY_LEVELS.index(granularity)
            elif granularity == 'lifetime':
                pass
            else:
                levels = ", ".join(self.GRANULARITY_LEVELS)
                raise ValueError("Granularity should be one of: lifetime, " + levels)

        dimension = self.GRANULARITY_DIMENSIONS[granularity]
        self.raw['dimensions'].insert(0, dimension)

        return self

    @utils.immutable
    def range(self, start=None, stop=None, months=0, days=0):
        """
        Return a new query that fetches metrics within a certain date range.

        ```python
        query.range('2014-01-01', '2014-06-30')
        ```

        If you don't specify a `stop` argument, the date range will end today. If instead
        you meant to fetch just a single day's results, try:

        ```python
        query.range('2014-01-01', days=1)
        ```

        More generally, you can specify that you'd like a certain number of days,
        starting from a certain date:

        ```python
        query.range('2014-01-01', months=3)
        query.range('2014-01-01', days=28)
        ```

        Note that if you don't specify a granularity (either through the `interval`
        method or through the `hourly`, `daily`, `weekly`, `monthly` or `yearly`
        shortcut methods) you will get only a single result, encompassing the
        entire date range, per metric.

        **Note:** it is currently not possible to easily specify that you'd like
        to query the last last full week(s), month(s) et cetera.
        This will be added sometime in the future.
        """

        start, stop = utils.date.range(start, stop, months, days)

        self.raw.update({
            'start_date': start,
            'end_date': stop,
        })

        return self


    @inspector.implements(range)
    def hourly(self, *vargs, **kwargs):
        return self.interval('hour').range(*vargs, **kwargs)

    @inspector.implements(range)
    def daily(self, *vargs, **kwargs):
        """
        Return a new query that fetches metrics within a certain date
        range, summarized by day. This method is identical to
        `CoreQuery#range` but it sets the default granularity to
        `granularity='day'`.
        """
        return self.interval('day').range(*vargs, **kwargs)

    @inspector.implements(range)
    def weekly(self, *vargs, **kwargs):
        """
        Return a new query that fetches metrics within a certain date
        range, summarized by week. This method is identical to
        `CoreQuery#range` but it sets the default granularity to
        `granularity='week'`.
        """
        return self.interval('week').range(*vargs, **kwargs)

    @inspector.implements(range)
    def monthly(self, *vargs, **kwargs):
        """
        Return a new query that fetches metrics within a certain date
        range, summarized by month. This method is identical to
        `CoreQuery#range` but it sets the default granularity to
        `granularity='month'`.
        """
        return self.interval('month').range(*vargs, **kwargs)

    @inspector.implements(range)
    def yearly(self, *vargs, **kwargs):
        """
        Return a new query that fetches metrics within a certain date
        range, summarized by year. This method is identical to
        `CoreQuery#range` but it sets the default granularity to
        `granularity='year'`.
        """
        return self.interval('year').range(*vargs, **kwargs)

    @inspector.implements(range)
    def lifetime(self, *vargs, **kwargs):
        return self.range(*vargs, **kwargs)

    @utils.immutable
    def step(self, maximum):
        """
        Return a new query with a maximum amount of results to be returned
        in any one request, without implying that we should stop
        fetching beyond that limit (unlike `CoreQuery#limit`.)

        Useful in debugging pagination functionality.

        Perhaps also useful when you  want to be able to decide whether to
        continue fetching data, based  on the data you've already received.
        """

        self.raw['max_results'] = maximum
        return self

    @utils.immutable
    def limit(self, *_range):
        """
        Return a new query, limited to a certain number of results.

        ```python
        # first 100
        query.limit(100)
        # 50 to 60
        query.limit(50, 10)
        ```

        Please note carefully that Google Analytics uses
        1-indexing on its rows.
        """

        # uses the same argument order as
        # LIMIT in a SQL database
        if len(_range) == 2:
            start, maximum = _range
        else:
            start = 1
            maximum = _range[0]

        self.meta['limit'] = maximum

        self.raw.update({
            'start_index': start,
            'max_results': maximum,
        })
        return self

    @utils.immutable
    def segment_sequence(self, followed_by=False, immediately_followed_by=False, first=False):
        # sequences are just really hard to "simplify" because so much is possible

        if followed_by or immediately_followed_by:
            method = 'sequence'
        else:
            method = 'condition'

        raise NotImplementedError()

    @utils.immutable
    def segment(self, value=None, scope=None, metric_scope=None, **selection):
        """
        Return a new query, limited to a segment of all users or sessions.

        Accepts segment objects, filtered segment objects and segment names:

        ```python
        query.segment(account.segments['browser'])
        query.segment('browser')
        query.segment(account.segments['browser'].any('Chrome', 'Firefox'))
        ```

        Segment can also accept a segment expression when you pass
        in a `type` argument. The type argument can be either `users`
        or `sessions`. This is pretty close to the metal.

        ```python
        # will be translated into `users::condition::perUser::ga:sessions>10`
        query.segment('condition::perUser::ga:sessions>10', type='users')
        ```

        See the [Google Analytics dynamic segments documentation][segments]

        You can also use the `any`, `all`, `followed_by` and
        `immediately_followed_by` functions in this module to
        chain together segments.

        Everything about how segments get handled is still in flux.
        Feel free to propose ideas for a nicer interface on
        the [GitHub issues page][issues]

        [segments]: https://developers.google.com/analytics/devguides/reporting/core/v3/segments#reference
        [issues]: https://github.com/debrouwere/google-analytics/issues
        """

        """
        Technical note to self about segments:

        * users or sessions
        * sequence or condition
        * scope (perHit, perSession, perUser -- gte primary scope)

        Multiple conditions can be ANDed or ORed together; these two are equivalent

            users::condition::ga:revenue>10;ga:sessionDuration>60
            users::condition::ga:revenue>10;users::condition::ga:sessionDuration>60

        For sequences, prepending ^ means the first part of the sequence has to match
        the first session/hit/...

        * users and sessions conditions can be combined (but only with AND)
        * sequences and conditions can also be combined (but only with AND)

        sessions::sequence::ga:browser==Chrome;
        condition::perHit::ga:timeOnPage>5
        ->>
        ga:deviceCategory==mobile;ga:revenue>10;
        
        users::sequence::ga:deviceCategory==desktop
        ->>
        ga:deviceCategory=mobile;
        ga:revenue>100;
        condition::ga:browser==Chrome

        Problem: keyword arguments are passed as a dictionary, not an ordered dictionary!
        So e.g. this is risky

            query.sessions(time_on_page__gt=5, device_category='mobile', followed_by=True)
        """

        SCOPES = {
            'hits': 'perHit',
            'sessions': 'perSession',
            'users': 'perUser',
            }
        segments = self.meta.setdefault('segments', [])

        if value and len(selection):
            raise ValueError("Cannot specify a filter string and a filter keyword selection at the same time.")
        elif value:
            value = [self.api.segments.serialize(value)]
        elif len(selection):
            if not scope:
                raise ValueError("Scope is required. Choose from: users, sessions.")

            if metric_scope:
                metric_scope = SCOPES[metric_scope]

            value = select(self.api.columns, selection)
            value = [[scope, 'condition', metric_scope, condition] for condition in value]
            value = ['::'.join(filter(None, condition)) for condition in value]

        segments.append(value)
        self.raw['segment'] = utils.paste(segments, ',', ';')
        return self

    def users(self, **kwargs):
        return self.segment(scope='users', **kwargs)

    def sessions(self):
        return self.segment(scope='sessions', **kwargs)

    @utils.immutable
    def next(self):
        """
        Return a new query with a modified `start_index`.
        Mainly used internally to paginate through results.
        """
        step = self.raw.get('max_results', 1000)
        start = self.raw.get('start_index', 1) + step
        self.raw['start_index'] = start
        return self

    def get(self):
        """
        Run the query and return a `Report`.

        This method transparently handles paginated results, so even for results that
        are larger than the maximum amount of rows the Google Analytics API will
        return in a single request, or larger than the amount of rows as specified
        through `CoreQuery#step`, `get` will leaf through all pages,
        concatenate the results and produce a single Report instance.
        """

        cursor = self
        report = None
        is_complete = False
        is_enough = False

        while not (is_enough or is_complete):
            chunk = cursor.execute()

            if report:
                report.append(chunk.raw[0], cursor)
            else:
                report = chunk

            is_enough = len(report.rows) >= self.meta.get('limit', float('inf'))
            is_complete = chunk.is_complete
            cursor = cursor.next()

        return report



class RealTimeQuery(Query):
    """
    A query against the [Google Analytics Real Time API][realtime].

    **Note:** brand new! Please test and submit any issues to GitHub.

    [realtime]: https://developers.google.com/analytics/devguides/reporting/realtime/v3/reference/data/realtime#resource
    """

    @property
    def endpoint(self):
        return self.account.service.data().realtime()

    @utils.immutable
    def limit(self, maximum):
        """
        Return a new query, limited to a certain number of results.

        Unlike core reporting queries, you cannot specify a starting
        point for live queries, just the maximum results returned.

        ```python
        # first 50
        query.limit(50)
        ```
        """

        self.meta['limit'] = maximum
        self.raw.update({
            'max_results': maximum,
        })
        return self

    def get(self):
        return self.execute()


def describe(profile, description):
    """
    Generate a query by describing it as a series of actions
    and parameters to those actions. These map directly
    to Query methods and arguments to those methods.

    This is an alternative to the chaining interface.
    Mostly useful if you'd like to put your queries
    in a file, rather than in Python code.
    """
    api_type = description.pop('type', 'core')
    api = getattr(profile, api_type)
    return refine(api.query(), description)

def refine(query, description):
    """
    Refine a query from a dictionary of parameters that describes it.
    See `describe` for more information.
    """

    for attribute, arguments in description.items():
        if hasattr(query, attribute):
            attribute = getattr(query, attribute)
        else:
            raise ValueError("Unknown query method: " + attribute)

        # query descriptions are often automatically generated, and
        # may include empty calls, which we skip
        if utils.isempty(arguments):
            continue

        if callable(attribute):
            method = attribute
            if isinstance(arguments, dict):
                query = method(**arguments)
            elif isinstance(arguments, list):
                query = method(*arguments)
            else:
                query = method(arguments)
        else:
            setattr(attribute, arguments)
    
    return query
