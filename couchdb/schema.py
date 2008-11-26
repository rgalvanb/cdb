# -*- coding: utf-8 -*-
#
# Copyright (C) 2007-2008 Christopher Lenz
# All rights reserved.
#
# This software is licensed as described in the file COPYING, which
# you should have received as part of this distribution.

"""Mapping from raw JSON data structures to Python objects and vice versa.

>>> from couchdb import Server
>>> server = Server('http://localhost:5984/')
>>> db = server.create('python-tests')

To define a document schema, you declare a Python class inherited from
`Document`, and add any number of `Field` attributes:

>>> class Person(Document):
...     name = TextField()
...     age = IntegerField()
...     added = DateTimeField(default=datetime.now)
>>> person = Person(name='John Doe', age=42)
>>> person.store(db) #doctest: +ELLIPSIS
<Person ...>
>>> person.age
42

You can then load the data from the CouchDB server through your `Document`
subclass, and conveniently access all attributes:

>>> person = Person.load(db, person.id)
>>> old_rev = person.rev
>>> person.name
u'John Doe'
>>> person.age
42
>>> person.added                #doctest: +ELLIPSIS
datetime.datetime(...)

To update a document, simply set the attributes, and then call the ``store()``
method:

>>> person.name = 'John R. Doe'
>>> person.store(db)            #doctest: +ELLIPSIS
<Person ...>

If you retrieve the document from the server again, you should be getting the
updated data:

>>> person = Person.load(db, person.id)
>>> person.name
u'John R. Doe'
>>> person.rev != old_rev
True

>>> del server['python-tests']
"""

from calendar import timegm
from datetime import date, datetime, time
from decimal import Decimal
from time import strptime, struct_time

from couchdb.design import ViewDefinition

__all__ = ['Schema', 'Document', 'Field', 'TextField', 'FloatField',
           'IntegerField', 'LongField', 'BooleanField', 'DecimalField',
           'DateField', 'DateTimeField', 'TimeField', 'DictField', 'ListField']
__docformat__ = 'restructuredtext en'

DEFAULT = object()


class Field(object):
    """Basic unit for mapping a piece of data between Python and JSON.
    
    Instances of this class can be added to subclasses of `Document` to describe
    the schema of a document.
    """

    def __init__(self, name=None, default=None):
        self.name = name
        self.default = default

    def __get__(self, instance, owner):
        if instance is None:
            return self
        value = instance._data.get(self.name)
        if value is not None:
            value = self._to_python(value)
        elif self.default is not None:
            default = self.default
            if callable(default):
                default = default()
            value = default
        return value

    def __set__(self, instance, value):
        if value is not None:
            value = self._to_json(value)
        instance._data[self.name] = value

    def _to_python(self, value):
        return unicode(value)

    def _to_json(self, value):
        return self._to_python(value)


class SchemaMeta(type):

    def __new__(cls, name, bases, d):
        fields = {}
        for base in bases:
            if hasattr(base, '_fields'):
                fields.update(base._fields)
        for attrname, attrval in d.items():
            if isinstance(attrval, Field):
                if not attrval.name:
                    attrval.name = attrname
                fields[attrname] = attrval
        d['_fields'] = fields
        return type.__new__(cls, name, bases, d)


class Schema(object):
    __metaclass__ = SchemaMeta

    def __init__(self, **values):
        self._data = {}
        for attrname, field in self._fields.items():
            if attrname in values:
                setattr(self, attrname, values.pop(attrname))
            else:
                setattr(self, attrname, getattr(self, attrname))

    def __iter__(self):
        return iter(self._data)

    def __len__(self):
        return len(self._data or ())

    def __delitem__(self, name):
        del self._data[name]

    def __getitem__(self, name):
        return self._data[name]

    def __setitem__(self, name, value):
        self._data[name] = value

    def unwrap(self):
        return self._data

    def build(cls, **d):
        fields = {}
        for attrname, attrval in d.items():
            if not attrval.name:
                attrval.name = attrname
            fields[attrname] = attrval
        d['_fields'] = fields
        return type('AnonymousStruct', (cls,), d)
    build = classmethod(build)

    def wrap(cls, data):
        instance = cls()
        instance._data = data
        return instance
    wrap = classmethod(wrap)

    def _to_python(self, value):
        return self.wrap(value)

    def _to_json(self, value):
        return self.unwrap()


class View(object):
    r"""Descriptor that can be used to bind a view definition to a property of
    a `Document` class.
    
    >>> class Person(Document):
    ...     name = TextField()
    ...     age = IntegerField()
    ...     by_name = View('people', '''\
    ...         function(doc) {
    ...             emit(doc.name, doc.age);
    ...         }''')
    >>> Person.by_name
    <ViewDefinition '_view/people/by_name'>
    
    >>> print Person.by_name.map_fun
    function(doc) {
        emit(doc.name, doc.age);
    }
    
    That property can be used as a function, which will execute the view.
    
    >>> from couchdb import Database
    >>> db = Database('http://localhost:5984/python-tests')
    
    >>> Person.by_name(db, count=3)
    <ViewResults <PermanentView '_view/people/by_name'> {'count': 3}>
    
    Actual results produced are automatically wrapped in the `Document`
    subclass the descriptor is bound to. In this example, it would return
    instances of the `Person` class.
    """

    def __init__(self, design, map_fun, reduce_fun=None,
                 name=None, language='javascript', wrapper=DEFAULT,
                 **defaults):
        """Initialize the view descriptor.
        
        :param design: the name of the design document
        :param map_fun: the map function code
        :param reduce_fun: the reduce function code (optional)
        :param name: the actual name of the view in the design document, if
                     it differs from the name the descriptor is assigned to
        :param language: the name of the language used
        :param wrapper: an optional callable that should be used to wrap the
                        result rows
        :param defaults: default query string parameters to apply
        """
        self.design = design
        self.name = name
        self.map_fun = map_fun
        self.reduce_fun = reduce_fun
        self.language = language
        self.wrapper = wrapper
        self.defaults = defaults

    def __get__(self, instance, cls=None):
        if self.wrapper is DEFAULT:
            def wrapper(row):
                if row.doc is not None:
                    return cls.wrap(row.doc)
                data = row.value
                data['_id'] = row.id
                return cls.wrap(data)
        else:
            wrapper = self.wrapper
        return ViewDefinition(self.design, self.name, self.map_fun,
                              self.reduce_fun, language=self.language,
                              wrapper=wrapper, **self.defaults)


class DocumentMeta(SchemaMeta):

    def __new__(cls, name, bases, d):
        for attrname, attrval in d.items():
            if isinstance(attrval, View):
                if not attrval.name:
                    attrval.name = attrname
        return SchemaMeta.__new__(cls, name, bases, d)


class Document(Schema):
    __metaclass__ = DocumentMeta

    def __init__(self, id=None, **values):
        Schema.__init__(self, **values)
        if id is not None:
            self.id = id

    def __repr__(self):
        return '<%s %r@%r %r>' % (type(self).__name__, self.id, self.rev,
                                  dict([(k, v) for k, v in self._data.items()
                                        if k not in ('_id', '_rev')]))

    def _get_id(self):
        if hasattr(self._data, 'id'): # When data is client.Document
            return self._data.id
        return self._data.get('_id')
    def _set_id(self, value):
        if self.id is not None:
            raise AttributeError('id can only be set on new documents')
        self._data['_id'] = value
    id = property(_get_id, _set_id)

    def rev(self):
        if hasattr(self._data, 'rev'): # When data is client.Document
            return self._data.rev
        return self._data.get('_rev')
    rev = property(rev)

    def items(self):
        """Return the fields as a list of ``(name, value)`` tuples.
        
        This method is provided to enable easy conversion to native dictionary
        objects, for example to allow use of `schema.Document` instances with
        `client.Database.update`.
        
        >>> class Post(Document):
        ...     title = TextField()
        ...     author = TextField()
        >>> post = Post(id='foo-bar', title='Foo bar', author='Joe')
        >>> sorted(post.items())
        [('_id', 'foo-bar'), ('author', u'Joe'), ('title', u'Foo bar')]
        
        :return: a list of ``(name, value)`` tuples
        """
        retval = []
        if self.id is not None:
            retval.append(('_id', self.id))
            if self.rev is not None:
                retval.append(('_rev', self.rev))
        for name, value in self._data.items():
            if name not in ('_id', '_rev'):
                retval.append((name, value))
        return retval

    def load(cls, db, id):
        """Load a specific document from the given database.
        
        :param db: the `Database` object to retrieve the document from
        :param id: the document ID
        :return: the `Document` instance, or `None` if no document with the
                 given ID was found
        """
        doc = db.get(id)
        if doc is None:
            return None
        return cls.wrap(doc)
    load = classmethod(load)

    def store(self, db):
        """Store the document in the given database."""
        if self.id is None:
            docid = db.create(self._data)
            self._data = db.get(docid)
        else:
            db[self.id] = self._data
        return self

    def query(cls, db, map_fun, reduce_fun, language='javascript',
              eager=False, **options):
        """Execute a CouchDB temporary view and map the result values back to
        objects of this schema.
        
        Note that by default, any properties of the document that are not
        included in the values of the view will be treated as if they were
        missing from the document. If you'd rather want to load the full
        document for every row, set the `eager` option to `True`, but note that
        this will initiate a new HTTP request for every document, unless the
        `include_docs` option is explitly specified.
        """
        def _wrapper(row):
            if eager:
                if row.doc is not None:
                    return row.doc
                return cls.load(db, row.id)
            data = row.value
            data['_id'] = row.id
            return cls.wrap(data)
        return db.query(map_fun, reduce_fun=reduce_fun, language=language,
                        wrapper=_wrapper, **options)
    query = classmethod(query)

    def view(cls, db, viewname, eager=False, **options):
        """Execute a CouchDB named view and map the result values back to
        objects of this schema.
        
        Note that by default, any properties of the document that are not
        included in the values of the view will be treated as if they were
        missing from the document. If you'd rather want to load the full
        document for every row, set the `eager` option to `True`, but note that
        this will initiate a new HTTP request for every document, unless the
        `include_docs` option is explitly specified.
        """
        def _wrapper(row):
            if eager:
                if row.doc is not None:
                    return row.doc
                return cls.load(db, row.id)
            data = row.value
            data['_id'] = row.id
            return cls.wrap(data)
        return db.view(viewname, wrapper=_wrapper, **options)
    view = classmethod(view)


class TextField(Field):
    """Schema field for string values."""
    _to_python = unicode


class FloatField(Field):
    """Schema field for float values."""
    _to_python = float


class IntegerField(Field):
    """Schema field for integer values."""
    _to_python = int


class LongField(Field):
    """Schema field for long integer values."""
    _to_python = long


class BooleanField(Field):
    """Schema field for boolean values."""
    _to_python = bool


class DecimalField(Field):
    """Schema field for decimal values."""

    def _to_python(self, value):
        return Decimal(value)

    def _to_json(self, value):
        return unicode(value)


class DateField(Field):
    """Schema field for storing dates.
    
    >>> field = DateField()
    >>> field._to_python('2007-04-01')
    datetime.date(2007, 4, 1)
    >>> field._to_json(date(2007, 4, 1))
    '2007-04-01'
    >>> field._to_json(datetime(2007, 4, 1, 15, 30))
    '2007-04-01'
    """

    def _to_python(self, value):
        if isinstance(value, basestring):
            try:
                value = date(*strptime(value, '%Y-%m-%d')[:3])
            except ValueError, e:
                raise ValueError('Invalid ISO date %r' % value)
        return value

    def _to_json(self, value):
        if isinstance(value, datetime):
            value = value.date()
        return value.isoformat()


class DateTimeField(Field):
    """Schema field for storing date/time values.
    
    >>> field = DateTimeField()
    >>> field._to_python('2007-04-01T15:30:00Z')
    datetime.datetime(2007, 4, 1, 15, 30)
    >>> field._to_json(datetime(2007, 4, 1, 15, 30, 0, 9876))
    '2007-04-01T15:30:00Z'
    >>> field._to_json(date(2007, 4, 1))
    '2007-04-01T00:00:00Z'
    """

    def _to_python(self, value):
        if isinstance(value, basestring):
            try:
                value = value.split('.', 1)[0] # strip out microseconds
                value = value.rstrip('Z') # remove timezone separator
                timestamp = timegm(strptime(value, '%Y-%m-%dT%H:%M:%S'))
                value = datetime.utcfromtimestamp(timestamp)
            except ValueError, e:
                raise ValueError('Invalid ISO date/time %r' % value)
        return value

    def _to_json(self, value):
        if isinstance(value, struct_time):
            value = datetime.utcfromtimestamp(timegm(value))
        elif not isinstance(value, datetime):
            value = datetime.combine(value, time(0))
        return value.replace(microsecond=0).isoformat() + 'Z'


class TimeField(Field):
    """Schema field for storing times.
    
    >>> field = TimeField()
    >>> field._to_python('15:30:00')
    datetime.time(15, 30)
    >>> field._to_json(time(15, 30))
    '15:30:00'
    >>> field._to_json(datetime(2007, 4, 1, 15, 30))
    '15:30:00'
    """

    def _to_python(self, value):
        if isinstance(value, basestring):
            try:
                value = value.split('.', 1)[0] # strip out microseconds
                value = time(*strptime(value, '%H:%M:%S')[3:6])
            except ValueError, e:
                raise ValueError('Invalid ISO time %r' % value)
        return value

    def _to_json(self, value):
        if isinstance(value, datetime):
            value = value.time()
        return value.replace(microsecond=0).isoformat()


class DictField(Field):
    """Field type for nested dictionaries.
    
    >>> from couchdb import Server
    >>> server = Server('http://localhost:5984/')
    >>> db = server.create('python-tests')

    >>> class Post(Document):
    ...     title = TextField()
    ...     content = TextField()
    ...     author = DictField(Schema.build(
    ...         name = TextField(),
    ...         email = TextField()
    ...     ))

    >>> post = Post(title='Foo bar', author=dict(name='John Doe',
    ...                                          email='john@doe.com'))
    >>> post.store(db) #doctest: +ELLIPSIS
    <Post ...>
    >>> post = Post.load(db, post.id)
    >>> post.author.name
    u'John Doe'
    >>> post.author.email
    u'john@doe.com'

    >>> del server['python-tests']
    """
    def __init__(self, schema, name=None, default=None):
        Field.__init__(self, name=name, default=default or {})
        self.schema = schema

    def _to_python(self, value):
        return self.schema.wrap(value)

    def _to_json(self, value):
        if not isinstance(value, Schema):
            value = self.schema(**value)
        return value.unwrap()


class ListField(Field):
    """Field type for sequences of other fields.

    >>> from couchdb import Server
    >>> server = Server('http://localhost:5984/')
    >>> db = server.create('python-tests')

    >>> class Post(Document):
    ...     title = TextField()
    ...     content = TextField()
    ...     pubdate = DateTimeField(default=datetime.now)
    ...     comments = ListField(DictField(Schema.build(
    ...         author = TextField(),
    ...         content = TextField(),
    ...         time = DateTimeField()
    ...     )))

    >>> post = Post(title='Foo bar')
    >>> post.comments.append(author='myself', content='Bla bla',
    ...                      time=datetime.now())
    >>> len(post.comments)
    1
    >>> post.store(db) #doctest: +ELLIPSIS
    <Post ...>
    >>> post = Post.load(db, post.id)
    >>> comment = post.comments[0]
    >>> comment['author']
    'myself'
    >>> comment['content']
    'Bla bla'
    >>> comment['time'] #doctest: +ELLIPSIS
    '...T...Z'

    >>> del server['python-tests']
    """

    def __init__(self, field, name=None, default=None):
        Field.__init__(self, name=name, default=default or [])
        if type(field) is type:
            if issubclass(field, Field):
                field = field()
            elif issubclass(field, Schema):
                field = DictField(field)
        self.field = field

    def _to_python(self, value):
        return self.Proxy(value, self.field)

    def _to_json(self, value):
        return [self.field._to_json(item) for item in value]


    class Proxy(list):

        def __init__(self, list, field):
            self.list = list
            self.field = field

        def __lt__(self, other):
            return self.list < other

        def __le__(self, other):
            return self.list <= other

        def __eq__(self, other):
            return self.list == other

        def __ne__(self, other):
            return self.list != other

        def __gt__(self, other):
            return self.list > other

        def __ge__(self, other):
            return self.list >= other

        def __repr__(self):
            return repr(self.list)

        def __str__(self):
            return str(self.list)

        def __unicode__(self):
            return unicode(self.list)

        def __delitem__(self, index):
            del self.list[index]

        def __getitem__(self, index):
            return self.field._to_python(self.list[index])

        def __setitem__(self, index, value):
            self.list[index] = self.field._to_json(item)

        def __iter__(self):
            for index in range(len(self)):
                yield self[index]

        def __len__(self):
            return len(self.list)

        def __nonzero__(self):
            return bool(self.list)

        def append(self, *args, **kwargs):
            if args:
                assert len(args) == 1
                value = args[0]
            else:
                value = kwargs
            value = self.field._to_json(value)
            self.list.append(value)

        def extend(self, list):
            for item in list:
                self.append(item)
