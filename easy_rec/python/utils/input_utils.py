# -*- encoding:utf-8 -*-
# Copyright (c) Alibaba, Inc. and its affiliates.
import numpy as np
import tensorflow as tf

from easy_rec.python.protos.dataset_pb2 import DatasetConfig

if tf.__version__ >= '2.0':
  tf = tf.compat.v1


def get_type_defaults(field_type, default_val=''):
  type_defaults = {
      DatasetConfig.INT32: 0,
      DatasetConfig.INT64: 0,
      DatasetConfig.STRING: '',
      DatasetConfig.BOOL: False,
      DatasetConfig.FLOAT: 0.0,
      DatasetConfig.DOUBLE: 0.0
  }
  assert field_type in type_defaults, 'invalid type: %s' % field_type
  if default_val == '':
    default_val = type_defaults[field_type]
  if field_type == DatasetConfig.INT32:
    return int(default_val)
  elif field_type == DatasetConfig.INT64:
    return np.int64(default_val)
  elif field_type == DatasetConfig.STRING:
    return default_val
  elif field_type == DatasetConfig.BOOL:
    return default_val.lower() == 'true'
  elif field_type in [DatasetConfig.FLOAT]:
    return float(default_val)
  elif field_type in [DatasetConfig.DOUBLE]:
    return np.float64(default_val)

  return type_defaults[field_type]


def string_to_number(field, ftype, name=''):
  """Type conversion for parsing rtp fg input format.

  Args:
    field: field to be converted.
    ftype: field dtype set in DatasetConfig.
    name: field name for
  Returns: A name for the operation (optional).
  """
  tmp_field = field
  if ftype in [DatasetConfig.INT32, DatasetConfig.INT64]:
    # Int type is not supported in fg.
    # If you specify INT32, INT64 in DatasetConfig, you need to perform a cast at here.
    tmp_field = tf.string_to_number(
        field, tf.double, name='field_as_int_%s' % name)
    if ftype in [DatasetConfig.INT64]:
      tmp_field = tf.cast(tmp_field, tf.int64)
    else:
      tmp_field = tf.cast(tmp_field, tf.int32)
  elif ftype in [DatasetConfig.FLOAT]:
    tmp_field = tf.string_to_number(
        field, tf.float32, name='field_as_flt_%s' % name)
  elif ftype in [DatasetConfig.DOUBLE]:
    tmp_field = tf.string_to_number(
        field, tf.float64, name='field_as_flt_%s' % name)
  elif ftype in [DatasetConfig.BOOL]:
    tmp_field = tf.logical_or(tf.equal(field, 'True'), tf.equal(field, 'true'))
  else:
    assert 'invalid types: %s' % str(ftype)
  return tmp_field
