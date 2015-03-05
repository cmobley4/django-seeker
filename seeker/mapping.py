from django.conf import settings
from django.db import models
from elasticsearch_dsl.connections import connections
import elasticsearch_dsl as dsl
import logging
import six

logger = logging.getLogger(__name__)

def follow(obj, path):
    parts = path.split('__') if path else []
    for idx, part in enumerate(parts):
        if hasattr(obj, 'get_%s_display' % part):
            # If the root object has a method to get the display value for this part, we're done (the rest of the path, if any, is ignored).
            return getattr(obj, 'get_%s_display' % part)()
        else:
            # Otherwise, follow the yellow brick road.
            obj = getattr(obj, part, None)
            if isinstance(obj, models.Manager):
                # Managers are a special case - basically, branch and recurse over all objects with the remainder of the path. This means
                # any path with a Manager/ManyToManyField in it will always return a list, which I think makes sense.
                new_path = '__'.join(parts[idx + 1:])
                return [follow(o, new_path) for o in obj.all()]
    # We traversed the whole path and wound up with an object. If it's a Django model, use the unicode representation.
    if isinstance(obj, models.Model):
        return six.text_type(obj)
    return obj

class Indexable (object):
    _model = None

    @classmethod
    def queryset(cls):
        """
        The queryset to use when indexing or fetching model instances for this mapping. Defaults to ``cls._model.objects.all()``.
        A common use for overriding this method would be to add ``select_related()`` or ``prefetch_related()``.
        """
        return cls._model.objects.all()

    @classmethod
    def count(cls):
        """
        Returns the number of objects that will be indexed. By default, this just returns ``cls.queryset().count()``, but may
        be overridden to account for objects that won't be indexed.
        """
        return cls.queryset().count()

    @classmethod
    def should_index(cls, obj):
        """
        Called by :meth:`.get_objects` for every object returned by :meth:`.queryset` to determine if it should be indexed. The default
        implementation simply returns `True` for every object.
        """
        return True

    @classmethod
    def get_objects(cls, cursor=False):
        """
        A generator yielding object instances that will subsequently be indexed using :meth:`.get_data` and :meth:`.get_id`. This method
        calls :meth:`.queryset` and orders it by ``pk``, then slices the results according to :attr:`.batch_size`. This results
        in more queries, but avoids loading all objects into memory at once.

        :param cursor: If True, use a server-side cursor when fetching the results for better performance.
        """
        if cursor:
            from .compiler import CursorQuery
            qs = cls.queryset().order_by()
            # Swap out the Query object with a clone using our subclass.
            qs.query = qs.query.clone(klass=CursorQuery)
            for obj in qs.iterator():
                if cls.should_index(obj):
                    yield obj
        else:
            qs = cls.queryset().order_by('pk')
            total = qs.count()
            batch_size = getattr(settings, 'SEEKER_BATCH_SIZE', 1000)
            for start in range(0, total, batch_size):
                end = min(start + batch_size, total)
                for obj in qs.all()[start:end]:
                    if cls.should_index(obj):
                        yield obj

    @classmethod
    def get_id(cls, obj):
        """
        Given a Django model instance, returns an ID for ElasticSearch to use when indexing the specified object.
        Defaults to ``obj.pk``. Must be unique over :attr:`doc_type`.
        """
        return str(obj.pk)

    @classmethod
    def get_data(cls, obj):
        """
        Returns a dictionary mapping field names to values. Values are generated by first "following" any relations (i.e. traversing __ field notation),
        then calling :meth:`MappingType.to_elastic` on the resulting value.
        """
        data = {}
        for name in cls._doc_type.mapping:
            data[name] = follow(obj, name)
        return data

    @classmethod
    def label_for_field(cls, field_name):
        """
        Returns a human-readable label for the given field name. First checks to see if the field is defined on the Django model, and if so, uses
        that field's verbose_name.
        """
        if field_name.endswith('.raw'):
            field_name = field_name[:-4]
        try:
            f = cls._model._meta.get_field(field_name)
            return f.verbose_name.capitalize()
        except:
            return field_name.replace('_', ' ').capitalize()

    @classmethod
    def clear(cls, using=None, index=None):
        """
        Deletes the Elasticsearch mapping associated with this document type.
        """
        if index is None:
            index = cls._doc_type.index
        es = connections.get_connection(using or cls._doc_type.using)
        if es.indices.exists_type(index=index, doc_type=cls._doc_type.name):
            es.indices.delete_mapping(index=index, doc_type=cls._doc_type.name)
            es.indices.flush(index=index)

    @property
    def instance(self):
        return self.queryset().get(pk=self.id)

def document_field(field):
    defaults = {
        models.DateField: dsl.Date(),
        models.DateTimeField: dsl.Date(),
        models.IntegerField: dsl.Long(),
    }
    s = dsl.String(analyzer='snowball', fields={
        'raw': dsl.String(index='not_analyzed'),
    })
    return defaults.get(field.__class__, s)

def document_from_model(model_class, document_class=dsl.DocType, fields=None, exclude=None,
                        index=None, using='default', doc_type=None, mapping=None, field_factory=None):
    meta_parent = (object,)
    if hasattr(document_class, 'Meta'):
        meta_parent = (document_class.Meta, object)
    if index is None:
        index = getattr(settings, 'SEEKER_INDEX', 'seeker')
    if doc_type is None:
        doc_type = model_class.__name__.lower()
    if mapping is None:
        mapping = dsl.Mapping(doc_type)
    attrs = {
        'Meta': type('Meta', meta_parent, {
            'index': index,
            'using': using,
            'doc_type': doc_type,
            'mapping': mapping,
        }),
        '_model': model_class,
    }
    if field_factory is None:
        field_factory = document_field
    for f in model_class._meta.fields + model_class._meta.many_to_many:
        if not isinstance(f, models.AutoField):
            attrs[f.name] = field_factory(f)
    return type('%sDoc' % model_class.__name__, (document_class, Indexable), attrs)
