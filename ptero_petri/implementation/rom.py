# pylint: disable=W0212

from .container_utils import grouper
from uuid import uuid4

import functools
import hashlib
import redis
import json
import re
import time


KEY_DELIM = '|'

# Redis scripts (Instantiated at the bottom of the file)
_COPY_KEY_SCRIPT_BODY = """
if redis.call('EXISTS', KEYS[1]) == 1 then
    local data = redis.call('DUMP', KEYS[1]);
    redis.call('DEL', KEYS[2])
    return redis.call('RESTORE', KEYS[2], 0, data)
end
return "OK"
"""

_EXPIRE_KEY_SCRIPT_BODY = """
local ttl = ARGV[1]

for i,key in pairs (KEYS) do
    redis.call('EXPIRE', key, ttl)
end

return {0, "Success"}
"""

_EXPIRE_CHUNK_SIZE = 3000


def json_enc(obj):
    return json.dumps(obj)


def json_dec(text):
    if text is None:
        return None
    else:
        return json.loads(text)


def copy_key(connection, src, dst):
    return _COPY_KEY_SCRIPT(connection=connection, keys=[src, dst])


class RomIndexError(IndexError):
    pass


class NotInRedisError(KeyError):
    pass


class Script(object):
    def __init__(self, script_body=None):
        self.script_body = script_body
        self.script_hash = hashlib.sha1(script_body).hexdigest()

    def __call__(self, connection=None, keys=[], args=[]):
        if connection is None:
            raise TypeError("You must specify a connection")

        num_keys = len(keys)
        keys_and_args = keys + args
        try:
            return connection.evalsha(self.script_hash,
                    num_keys, *keys_and_args)
        except redis.exceptions.ResponseError:
            return connection.eval(self.script_body, num_keys, *keys_and_args)


class Property(object):
    def __init__(self, cls, **kwargs):
        if not issubclass(cls, Value):
            raise TypeError("Unknown redisom class %s" % str(cls))

        self.cls = cls
        self.kwargs = kwargs


UNINITIALIZED = object()
class Value(object):
    def __init__(self, connection=None, key=None,
            cacheable=False, immutable=False):
        if connection is None or key is None:
            raise TypeError("You must specify a connection and a key")

        # Immutability implies cacheability
        if immutable:
            cacheable = True

        self.connection = connection
        self.key = key
        self.cacheable = cacheable
        self.immutable = immutable

        self._cached_value = UNINITIALIZED

    @classmethod
    def create(cls, *args, **kwargs):
        return cls(*args, **kwargs)

    def copy(self, dst_key):
        copy_key(self.connection, self.key, dst_key)
        return self.__class__(connection=self.connection, key=dst_key)

    def setnx(self, value):
        return self.connection.setnx(self.key, self._encode(value))

    def delete(self):
        return self.connection.delete(self.key)


    def _encode(self, value):
        return value

    def _decode(self, value):
        return value


    def _get_decoded_value(self):
        result = self._get_raw_value()
        if result is None:
            raise NotInRedisError("Redis has no data for key (%s)."
                    % (self.key))
        else:
            return self._decode(result)

    def _get_raw_value(self):
        return self.connection.get(self.key)

    def _set_raw_value(self, new_value):
        return self.connection.set(self.key, self._encode(new_value))


    def _validate_immutable(self):
        if self.immutable:
            raise ValueError("Cannot modify immutable object %s(key=%s)"
                    % (self.__class__, self.key))


    def _value_getter(self):
        if self.cacheable:
            if self._cached_value is UNINITIALIZED:
                self._cached_value = self._get_decoded_value()

            return self._cached_value
        else:
            return self._get_decoded_value()

    def _value_setter(self, new_value):
        self._validate_immutable()
        return self._set_raw_value(new_value)


    def __int__(self):
        return int(self.value)

    def __float__(self):
        return float(self.value)

    def __str__(self):
        return str(self.value)


    value = property(_value_getter, _value_setter)


class Timestamp(Value):
    # Force timestamps to be immutable
    def __init__(self, *args, **kwargs):
        immutable = kwargs.pop("immutable", False)
        Value.__init__(self, *args, immutable=True, **kwargs)

    def _decode(self, value):
        return float(value)

    @property
    def now(self):
        sec, usec = self.connection.time()
        return "%d.%d" % (sec, usec)

    def setnx(self, value=None):
        if value is None:
            value = self.now

        if self.connection.setnx(self.key, value):
            return self._decode(value)
        else:
            return False


class Int(Value):
    def incr(self, *args, **kwargs):
        self._validate_immutable()
        return self.connection.incr(self.key, *args, **kwargs)

    def decr(self, *args, **kwargs):
        self._validate_immutable()
        return self.connection.decr(self.key, *args, **kwargs)

    def _encode(self, value):
        return int(value)

    def _decode(self, value):
        return int(value)


class String(Value):
    def _encode(self, value):
        return str(value)


class Set(Value):
    def _get_raw_value(self):
        return self.connection.smembers(self.key)

    def _set_raw_value(self, val):
        pipe = self.connection.pipeline()
        pipe.delete(self.key)
        if val:
            pipe.sadd(self.key, *val)
        pipe.execute()

    def add(self, val):
        return self.connection.sadd(self.key, val)

    def add_return_size(self, val):
        pipe = self.connection.pipeline()
        pipe.sadd(self.key, val)
        pipe.scard(self.key)
        added, size = pipe.execute()
        return added, size

    def remove(self, val):
        removed, size = self.discard(val)
        if not removed:
            raise KeyError("Set (%s) doesn't contain value %s" %
                    (self.key, val))
        return removed, size

    def discard(self, val):
        pipe = self.connection.pipeline()
        pipe.srem(self.key, val)
        pipe.scard(self.key)
        removed, size = pipe.execute()
        return removed, size

    def update(self, vals):
        return self.connection.sadd(self.key, *vals)

    def __iter__(self):
        return self.value.__iter__()

    def __len__(self):
        return self.connection.scard(self.key)


class EncodableContainer(Value):
    def __init__(self, *args, **kwargs):
        self._value_encoder = kwargs.pop("value_encoder", None)
        self._value_decoder = kwargs.pop("value_decoder", None)

        cacheable = kwargs.pop("cacheable", False)
        immutable = kwargs.pop("immutable", False)

        if cacheable:
            raise NotImplementedError(
                    "Cacheable containers are not supported")
        if immutable:
            raise NotImplementedError(
                    "Immutable containers are not supported")

        Value.__init__(self, *args, **kwargs)

    def copy(self, dst_key):
        copy_key(self.connection, self.key, dst_key)
        return self.__class__(value_encoder=self._value_encoder,
                value_decoder=self._value_decoder,
                connection=self.connection, key=dst_key)

    def _encode_value(self, v):
        if self._value_encoder is None:
            return v
        else:
            return self._value_encoder(v)

    def _decode_value(self, v):
        if self._value_decoder is None:
            return v
        else:
            return self._value_decoder(v)


class List(EncodableContainer):
    def _encode_values(self, values):
        if self._value_encoder is None:
            return values
        else:
            encoder = self._value_encoder
            return [encoder(v) for v in values]

    def _decode_values(self, values):
        if self._value_decoder is None:
            return values
        else:
            decoder = self._value_decoder
            return [decoder(v) for v in values]

    def __getitem__(self, idx):
        try:
            idx = int(idx)
        except:
            raise TypeError("list indices must be integers, not str")

        result = self.connection.lindex(self.key, idx)
        # NOTE We accept the constraint that list values cannot be None here
        if result is None:
            raise RomIndexError("list index out of range "
                    "(key=%s, index=%d)" % (self.key, idx))
        return self._decode_value(result)

    def __setitem__(self, idx, val):
        try:
            return self.connection.lset(self.key, idx, self._encode_value(val))
        except redis.ResponseError:
            raise RomIndexError ("list index out of range "
                    "(key=%s, size=%d, index=%d)" % (self.key, len(self), idx))

    def __len__(self):
        return self.connection.llen(self.key)

    def _get_raw_value(self):
        return self._decode_values(self.connection.lrange(self.key, 0, -1))

    def _set_raw_value(self, val):
        self.connection.delete(self.key)
        if val:
            return self.connection.rpush(self.key, *self._encode_values(val))

    def append(self, val):
        return self.extend([val])

    def extend(self, vals):
        # Something in the redis module doesn't work well with
        # generators, so we need an actual list
        encoded_vals = self._encode_values(vals)
        if encoded_vals:
            return self.connection.rpush(self.key, *encoded_vals)


class Hash(EncodableContainer):
    def _encode(self, d):
        if self._value_encoder is None:
            return d
        else:
            encoder = self._value_encoder
            return dict((k, encoder(v)) for k, v in d.iteritems())

    def _decode(self, d):
        if self._value_encoder is None:
            return d
        else:
            decoder = self._value_decoder
            return dict((k, decoder(v)) for k, v in d.iteritems())


    def _decode_values(self, values):
        if self._value_decoder is None:
            return values
        else:
            decoder = self._value_decoder
            return [decoder(v) for v in values]


    def incrby(self, key, n):
        return self.connection.hincrby(self.key, key, n)


    def _get_raw_value(self):
        return self.connection.hgetall(self.key)

    def _set_raw_value(self, d):
        if d:
            pipe = self.connection.pipeline()
            pipe.delete(self.key)
            pipe.hmset(self.key, self._encode(d))
            pipe.execute()
        else:
            return self.connection.delete(self.key)


    def __setitem__(self, hkey, val):
        return self.connection.hset(self.key, hkey, self._encode_value(val))

    def setnx(self, hkey, val):
        return self.connection.hsetnx(self.key, hkey, self._encode_value(val))


    def __getitem__(self, hkey):
        result = self._get_raw(hkey)
        if result is None:
            raise KeyError("Hash (%s) has no key '%s'" % (self.key, hkey))
        return self._decode_value(result)

    def _get_raw(self, hkey):
        return self.connection.hget(self.key, hkey)

    def get(self, hkey, default=None):
        result = self._get_raw(hkey)
        if result is None:
            return default
        else:
            return self._decode_value(result)

    def __delitem__(self, hkey):
        pipe = self.connection.pipeline()
        pipe.hexists(self.key, hkey)
        pipe.hdel(self.key, hkey)
        exists, rv = pipe.execute()
        if not exists:
            raise KeyError("Hash (%s) has no key '%s'" % (self.key, hkey))
        return rv

    def __len__(self):
        return self.connection.hlen(self.key)

    def __contains__(self, key):
        return self.connection.hexists(self.key, key)

    def keys(self):
        return self.connection.hkeys(self.key)

    def values(self, keys=None):
        if keys is None:
            raw = self.connection.hvals(self.key)
        else:
            raw = self.connection.hmget(self.key, keys)
        return self._decode_values(raw)

    def update(self, other):
        if not other:
            return None
        return self.connection.hmset(self.key, self._encode(other))

    def iteritems(self):
        return self.value.iteritems()


def _make_key(*args):
    return KEY_DELIM.join(map(str, args))


class ObjectMeta(type):
    _class_registry = {}
    _class_info_re = re.compile(r"^[A-Za-z0-9_.]+:[A-Za-z0-9_]+$")
    def __new__(mcs, class_name, bases, class_dict):
        cls = type.__new__(mcs, class_name, bases, class_dict)

        members = {}
        for base in bases:
            members.update(base.__dict__)
        members.update(class_dict)

        rom_dict = {
                "_rom_scripts": Script,
                "_rom_properties": Property,
                }

        for hidden_variable, hidden_class in rom_dict.items():
            setattr(cls, hidden_variable, {})
            this_hv = getattr(cls, hidden_variable)

            for base in bases:
                this_hv.update(getattr(base, hidden_variable, {}))

            for name, value in members.iteritems():
                if isinstance(value, hidden_class):
                    this_hv[name] = value
                    delattr(cls, name)

        class_info = "%s:%s" % (cls.__module__, cls.__name__)
        mcs._class_registry[class_info] = cls
        cls._info = class_info


        return cls

    def get_class(cls, class_info):
        try:
            result = cls._class_registry[class_info]
        except KeyError:
            if cls._class_info_re.match(class_info) is None:
                raise TypeError("Improperly formatted class_info (%s) must "
                        "be formatted as <module>:<class_name>" % class_info)

            class_module, class_name = class_info.split(':')
            __import__(class_module)
            try:
                result = cls._class_registry[class_info]
            except KeyError:
                raise ImportError("Expected to register class %s when loading "
                        "module %s but failed to." % (class_name, class_module))
        return result


class Object(object):
    __metaclass__ = ObjectMeta

    def __init__(self, connection=None, key=None):
        if connection is None or key is None:
            raise TypeError("You must specify a connection and a key")
        self.__dict__.update({
            "key": key,
            "_cache": {},
            "_class_info": String(connection=connection, key=key),
            "connection": connection,
        })

        for name, script in self._rom_scripts.iteritems():
            self.__dict__[name] = functools.partial(script, self.connection)

    def copy(self, dst_key):
        target = self.__class__.create(connection=self.connection, key=dst_key)

        for prop_name in self._rom_properties:
            src_prop = getattr(self, prop_name)
            tgt_prop = getattr(target, prop_name)
            src_prop.copy(tgt_prop.key)

        return target

    def __getattr__(self, name):
        # fallback for when lookup in __dict__ fails
        try:
            result = self._cache[name]
        except KeyError:
            try:
                propdef = self._rom_properties[name]
            except KeyError:
                raise AttributeError("No such property or relation '%s'"
                        " on class %s" % (name, self.__class__.__name__))
            else:
                cls = propdef.cls
                prop = cls.create(connection=self.connection,
                        key=self.subkey(name), **propdef.kwargs)
                self._cache[name] = prop

            result = prop

        return result

    def __setattr__(self, name, value):
        # is always called on attribute setting
        if name in self._rom_properties:
            getattr(self, name).value = value
        else:
            object.__setattr__(self, name, value)

    def __delattr__(self, name):
        if name in self._rom_properties:
            getattr(self, name).delete()

        if name in self._cache:
            del self._cache[name]

    def exists(self):
        try:
            class_info = self._class_info.value
        except NotInRedisError:
            return False

        if class_info != self._info:
            raise TypeError("Classinfo for %s isn't correct, found '%s' "
                    "expected '%s'" % (self.key, class_info, self._info))

        return True

    @classmethod
    def make_default_key(cls):
        return _make_key(KEY_DELIM + cls.__module__, cls.__name__,
                        uuid4().hex)

    def _on_create(self):
        pass

    @classmethod
    def create(cls, connection=None, key=None, **kwargs):
        return create_object(cls, connection, key, **kwargs)

    @classmethod
    def get(cls, connection=None, key=None):
        if connection is None or key is None:
            raise TypeError('get requires connection and key to be specified.')
        obj = cls(connection=connection, key=key)
        if not obj.exists():
            raise KeyError("Object not found: class=%s key=%s" % (cls, key))
        return obj

    def method_descriptor(self, method_name):
        method = getattr(self, method_name, None)
        if not hasattr(method, '__call__'):
            raise AttributeError("Unknown instance method %s for class %s"
                                 % (method_name, self.__class__.__name__))
        return {"object_key": self.key, "method_name": method_name}

    def subkey(self, *args):
        return _make_key(self.key, *args)

    def associated_iterkeys(self):
        for name in self._rom_properties.iterkeys():
            yield getattr(self, name).key
        yield self.key
        for name in self.additional_associated_iterkeys():
            yield name

    def additional_associated_iterkeys(self):
        # override as a generator to add to .associated_iterkeys
        return []

    def delete(self):
        keys = list(self.associated_iterkeys())
        self.connection.delete(*keys)

    def expire(self, seconds):
        key_groups = grouper(_EXPIRE_CHUNK_SIZE, self.associated_iterkeys())
        for group in key_groups:
            _EXPIRE_KEY_SCRIPT(connection=self.connection,
                    keys=[key for key in group if key is not None],
                    args=[seconds])


def get_object(connection=None, key=None):
    if connection is None or key is None:
        raise TypeError("You must specify connection and key")

    class_info = connection.get(key)
    if class_info is None:
        raise NotInRedisError("No object found in redis with key (%s)" % key)

    cls = Object.get_class(class_info)
    obj = cls(connection=connection, key=key)
    return obj


def create_object(cls, connection=None, key=None, **kwargs):
    if key is None:
        key = cls.make_default_key()

    obj = cls(connection=connection, key=key)
    obj._class_info.value = cls._info

    for k, v in kwargs.iteritems():
        if k not in obj._rom_properties:
            raise AttributeError("Unknown attribute %s" % k)
        if v is not None:
            # Forcibly initialize immutable values at creation
            getattr(obj, k)._set_raw_value(v)

    obj._on_create()

    return obj


def invoke_instance_method(connection, method_descriptor, **kwargs):
    obj = get_object(connection, method_descriptor['object_key'])
    method = getattr(obj, method_descriptor['method_name'], None)
    if method == None:
        raise AttributeError("Invalid method for class %s: %s"
                             % (obj.__class__.__name__,
                             method_descriptor["method_name"]))
    return method(**kwargs)


_COPY_KEY_SCRIPT = Script(_COPY_KEY_SCRIPT_BODY)
_EXPIRE_KEY_SCRIPT = Script(_EXPIRE_KEY_SCRIPT_BODY)
