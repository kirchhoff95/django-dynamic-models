"""Provides the base models of dynamic model schema classes.

Abstract models should be subclassed to provide extra functionality, but they
are perfectly usable without adding any additional fields.

`AbstractModelSchema` -- base model that defines dynamic models
`AbstractFieldSchema` -- base model for defining fields to use on dynamic models
`DynamicModelField`   -- through model for attaching fields to dynamic models
"""
from django.db import models
from django.apps import apps
from django.utils.functional import cached_property
from django.utils.text import slugify
from django.contrib.contenttypes.models import ContentType
from django.contrib.contenttypes.fields import GenericForeignKey
from model_utils import Choices, FieldTracker

from . import utils
from . import signals
from . import exceptions
from . import factory


# TODO: Move this to __init_subclass__
class ModelSchemaBase(models.base.ModelBase):
    def __new__(cls, name, bases, attrs, **kwargs):
        model = super().__new__(cls, name, bases, attrs, **kwargs)
        if not model._meta.abstract:
            signals.connect_model_schema_handlers(model)
        return model


# TODO: support table name changes
class AbstractModelSchema(models.Model, metaclass=ModelSchemaBase):
    """Base model for the dynamic model schema table.

    Fields:
    `name`     -- used to generate the `model_name` and `table_name` properties
    `modified` -- a timestamp of the last time the instance wsa changed
    """
    name = models.CharField(max_length=32, unique=True, editable=False)
    modified = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True

    @property
    def app_label(self):
        return self.__class__._meta.app_label

    @property
    def model_name(self):
        return self.name.title().replace(' ', '')

    @property
    def table_name(self):
        parts = [self.app_label, self.__class__.__name__, slugify(self.name)]
        return '_'.join(parts).replace('-', '_')

    @cached_property
    def factory(self):
        return factory.ModelFactory(self)

    @property
    def model_fields(self):
        """Return the `DynamicModelField` instances related to this schema."""
        return self._model_fields_queryset().prefetch_related('field')

    def _model_fields_queryset(self):
        model_ct = ContentType.objects.get_for_model(self)
        return DynamicModelField.objects.filter(
            model_content_type=model_ct,
            model_id=self.id
        )

    def add_field(self, field, **options):
        """Add a field to the model schema with the constraint options.

        Field options are passed as keyword args:
        `required`   -- sets NULL constraint on the generated field
        `unique`     -- sets UNIQUE constraint on the generated field
        `max_length` -- sets Django's max_length option on generated CharFields
        """
        return DynamicModelField.objects.create(
            model=self,
            field=field,
            **options
        )

    def remove_field(self, field):
        """Remove a field from this model schema."""
        self._get_field(field).delete()

    def update_field(self, field, **options):
        """Updates the given model field with new options.

        Does not perform an UPDATE query so the schema changing signal is
        properly triggered. Raise DoesNotExist if the field is not found.
        """
        old_field = self._get_field(field)
        updated_field = self._set_field_options(old_field, options)
        updated_field.save()
        return updated_field

    def _get_field(self, field):
        field_ct = ContentType.objects.get_for_model(field)
        return self._model_fields_queryset().get(
            field_content_type=field_ct,
            field_id=field.id
        )

    def _set_field_options(self, field, options):
        for option, value in options.items():
            setattr(field, option, value)
        return field

    def as_model(self):
        """Return a dynamic model represeted by this schema instance."""
        try:
            return self._try_cached_model()
        except exceptions.ModelDoesNotExistError:
            return self.factory.build()

    def _try_cached_model(self):
        try:
            return self._cached_model()
        except exceptions.OutdatedModelError:
            self._unregister_model()

    def _cached_model(self):
        model = utils.get_model(self.app_label, self.model_name)
        self._check_model_is_current(model)
        return model

    def _unregister_model(self):
        try:
            del apps.all_models[self.app_label][self.model_name]
        except KeyError as err:
            raise exceptions.ModelDoesNotExistError() from err
        else:
            signals.disconnect_dynamic_model(self.model_name)

    def _check_model_is_current(self, model):
        if not self._has_current_schema(model):
            raise exceptions.OutdatedModelError()

    def _has_current_schema(self, model):
        return model._declared >= self.modified # pylint: disable=protected-access



class AbstractFieldSchema(models.Model):
    """Base model for dynamic field definitions.

    Data type choices are stored in the DATA_TYPES class attribute. DATA_TYPES
    should be a valid `choices` object. Each data type should have a key set in
    FIELD_TYPES mapping to the constructor of a Django `Field` class.
    """
    # TODO: support foreign keys
    DATA_TYPES = Choices(
        ('char', 'short text'),
        ('text', 'long text'),
        ('int', 'integer'),
        ('float', 'float'),
        ('bool', 'boolean'),
        ('date', 'date')
    )

    FIELD_TYPES = {
        DATA_TYPES.char: models.CharField,
        DATA_TYPES.text: models.TextField,
        DATA_TYPES.int: models.IntegerField,
        DATA_TYPES.float: models.FloatField,
        DATA_TYPES.date: models.DateTimeField,
        DATA_TYPES.bool: models.BooleanField
    }

    assert set(dt[0] for dt in DATA_TYPES).issubset(FIELD_TYPES.keys()),\
        "All DATA_TYPES must be present in the FIELD_TYPES map"

    name = models.CharField(max_length=32, unique=True, editable=False)
    data_type = models.CharField(
        max_length=8,
        choices=DATA_TYPES,
        editable=False
    )
    class Meta:
        abstract = True

    @property
    def column_name(self):
        """Return the name of the database column created by this field."""
        return slugify(self.name).replace('-', '_')

    @property
    def constructor(self):
        """Return a callable that constructs a Django Field instance."""
        return self.__class__.FIELD_TYPES[self.data_type]

    def as_field(self, **options):
        """Returns an unassociated Django Field instance."""
        return self.constructor(db_column=self.column_name, **options) # pylint: disable=not-callable

    @classmethod
    def _data_types_keys(cls):
        for data_type in cls.DATA_TYPES:
            yield data_type[0]


# Export default data types from the class
DefaultDataTypes = AbstractFieldSchema.DATA_TYPES # pylint: disable=invalid-name


class DynamicModelField(models.Model):
    """Through table for model schema objects to field schema objects.

    This model should only be interacted with by the interface provided in the
    AbstractModelSchema base class. It is responsible for generating model
    fields with customized constraints.
    """
    model_content_type = models.ForeignKey(
        ContentType,
        on_delete=models.CASCADE,
        related_name='model_content_types',
        editable=False
    )
    model_id = models.PositiveIntegerField(editable=False)
    model = GenericForeignKey('model_content_type', 'model_id')

    field_content_type = models.ForeignKey(
        ContentType,
        on_delete=models.CASCADE,
        related_name='field_content_types',
        editable=False
    )
    field_id = models.PositiveIntegerField(editable=False)
    field = GenericForeignKey('field_content_type', 'field_id')

    # TODO: add index and default value options
    # TODO: allow changing NULL fields
    null = models.BooleanField(default=True)
    unique = models.BooleanField(default=False)
    max_length = models.PositiveIntegerField(null=True)

    tracker = FieldTracker(fields=['required', 'unique', 'max_length'])

    class Meta:
        unique_together = (
            'model_content_type',
            'model_id',
            'field_content_type',
            'field_id'
        ),

    @property
    def column_name(self):
        return self.field.column_name

    def as_field(self):
        """Return the Django model field instance with configured constraints."""
        options = {'null': not self.null, 'unique': self.unique}
        self._add_max_length_option(options)
        return self.field.as_field(**options)

    def save(self, **kwargs): # pylint: disable=arguments-differ
        self._check_null_is_valid()
        if self._schema_updated():
            self.model.save()
        super().save(**kwargs)

    def _add_max_length_option(self, options):
        if self._requires_max_length():
            options['max_length'] = self.max_length or utils.default_max_length()
        return options

    def _requires_max_length(self):
        return self.field.constructor is models.CharField

    def _check_null_is_valid(self):
        if self.tracker.previous('null') is True and not self.null:
            raise exceptions.NullFieldChangedError(self.column_name)

    def _schema_updated(self):
        return not self.id or self.tracker.changed()
