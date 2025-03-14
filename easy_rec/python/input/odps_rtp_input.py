# -*- encoding:utf-8 -*-
# Copyright (c) Alibaba, Inc. and its affiliates.
import logging

import tensorflow as tf

from easy_rec.python.input.input import Input
from easy_rec.python.utils.input_utils import string_to_number

try:
  import pai
except Exception:
  pass


class OdpsRTPInput(Input):
  """RTPInput for parsing rtp fg new input format on odps.

  Our new format(csv in table) of rtp output:
     label0, item_id, ..., user_id, features
  For the feature column, features are separated by ,
     multiple values of one feature are separated by , such as:
     ...20beautysmartParis...
  The features column and labels are specified by data_config.selected_cols,
     columns are selected by names in the table
     such as: clk,features, the last selected column is features, the first
     selected columns are labels
  """

  def __init__(self,
               data_config,
               feature_config,
               input_path,
               task_index=0,
               task_num=1):
    super(OdpsRTPInput, self).__init__(data_config, feature_config, input_path,
                                       task_index, task_num)
    logging.info('input_fields: %s label_fields: %s' %
                 (','.join(self._input_fields), ','.join(self._label_fields)))

  def _parse_table(self, *fields):
    fields = list(fields)
    labels = fields[:-1]

    # only for features, labels excluded
    record_types = [
        t for x, t in zip(self._input_fields, self._input_field_types)
        if x not in self._label_fields
    ]
    # assume that the last field is the generated feature column
    print('field_delim = %s, input_field_name = %d' %
          (self._data_config.separator, len(record_types)))
    fields = tf.string_split(
        fields[-1], self._data_config.separator, skip_empty=False)
    tmp_fields = tf.reshape(fields.values, [-1, len(record_types)])
    fields = []
    for i in range(len(record_types)):
      field = string_to_number(tmp_fields[:, i], record_types[i], i)
      fields.append(field)

    field_keys = [x for x in self._input_fields if x not in self._label_fields]
    effective_fids = [field_keys.index(x) for x in self._effective_fields]
    inputs = {field_keys[x]: fields[x] for x in effective_fids}

    for x in range(len(self._label_fields)):
      inputs[self._label_fields[x]] = labels[x]
    return inputs

  def _build(self, mode, params):
    if type(self._input_path) != list:
      self._input_path = [x for x in self._input_path.split(',')]

    record_defaults = [
        self.get_type_defaults(t, v)
        for x, t, v in zip(self._input_fields, self._input_field_types,
                           self._input_field_defaults)
        if x in self._label_fields
    ]
    # the actual features are in one single column
    record_defaults.append(
        self._data_config.separator.join([
            str(self.get_type_defaults(t, v))
            for x, t, v in zip(self._input_fields, self._input_field_types,
                               self._input_field_defaults)
            if x not in self._label_fields
        ]))
    selected_cols = self._data_config.selected_cols \
        if self._data_config.selected_cols else None

    if self._data_config.pai_worker_queue and \
        mode == tf.estimator.ModeKeys.TRAIN:
      logging.info('pai_worker_slice_num = %d' %
                   self._data_config.pai_worker_slice_num)
      work_queue = pai.data.WorkQueue(
          self._input_path,
          num_epochs=self.num_epochs,
          shuffle=self._data_config.shuffle,
          num_slices=self._data_config.pai_worker_slice_num * self._task_num)
      que_paths = work_queue.input_dataset()
      dataset = tf.data.TableRecordDataset(
          que_paths,
          record_defaults=record_defaults,
          selected_cols=selected_cols)
    else:
      dataset = tf.data.TableRecordDataset(
          self._input_path,
          record_defaults=record_defaults,
          selected_cols=selected_cols,
          slice_id=self._task_index,
          slice_count=self._task_num)

    if mode == tf.estimator.ModeKeys.TRAIN:
      if self._data_config.shuffle:
        dataset = dataset.shuffle(
            self._data_config.shuffle_buffer_size,
            seed=2020,
            reshuffle_each_iteration=True)
      dataset = dataset.repeat(self.num_epochs)
    else:
      dataset = dataset.repeat(1)

    dataset = dataset.batch(batch_size=self._data_config.batch_size)

    dataset = dataset.map(
        self._parse_table,
        num_parallel_calls=self._data_config.num_parallel_calls)

    # preprocess is necessary to transform data
    # so that they could be feed into FeatureColumns
    dataset = dataset.map(
        map_func=self._preprocess,
        num_parallel_calls=self._data_config.num_parallel_calls)

    dataset = dataset.prefetch(buffer_size=self._prefetch_size)

    if mode != tf.estimator.ModeKeys.PREDICT:
      dataset = dataset.map(lambda x:
                            (self._get_features(x), self._get_labels(x)))
    else:
      dataset = dataset.map(lambda x: (self._get_features(x)))
    return dataset
