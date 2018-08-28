#! /usr/bin/python

import os
import sys
import argparse
import json

import numpy as np
import luigi
import nifty.distributed as ndist

import cluster_tools.utils.volume_utils as vu
import cluster_tools.utils.function_utils as fu
from cluster_tools.cluster_tasks import SlurmTask, LocalTask, LSFTask


class BlockEdgeFeaturesBase(luigi.Task):
    """ Block edge feature base class
    """

    task_name = 'block_edge_features'
    src_file = os.path.abspath(__file__)

    # input and output volumes
    input_path = luigi.Parameter()
    input_key = luigi.Parameter()
    labels_path = luigi.Parameter()
    labels_key = luigi.Parameter()
    graph_path = luigi.Parameter()
    output_path = luigi.Parameter()
    dependency = luigi.TaskParameter()

    def requires(self):
        return self.dependency

    @staticmethod
    def default_task_config():
        # we use this to get also get the common default config
        config = LocalTask.default_task_config()
        config.update({'offsets': None, 'filters': None, 'sigmas': None, 'halo': [0, 0, 0]})
        return config

    def clean_up_for_retry(self, block_list):
        # TODO does this work with the mixin pattern?
        super().clean_up_for_retry(block_list)
        # TODO remove any output of failed blocks because it might be corrupted

    def run(self):
        # get the global config and init configs
        self.make_dirs()
        shebang, block_shape, roi_begin, roi_end = self.global_config_values()
        self.init(shebang)

        # load the task config
        config = self.get_task_config()

        # require output group
        with vu.file_reader(self.output_path) as f:
            f.require_group('blocks')

        # TODO make the scale at which we extract features accessible
        # update the config with input and output paths and keys
        # as well as block shape
        config.update({'input_path': self.input_path, 'input_key': self.input_key,
                       'labels_path': self.labels_path, 'labels_key': self.labels_key,
                       'output_path': self.output_path, 'block_shape': block_shape,
                       'graph_block_prefix': os.path.join(self.graph_path, 's0',
                                                          'sub_graphs', 'block_')})

        if self.n_retries == 0:
            # get shape and make block config
            shape = vu.get_shape(self.input_path, self.input_key)
            if len(shape) == 4:
                shape = shape[1:]
            block_list = vu.blocks_in_volume(shape, block_shape, roi_begin, roi_end)
        else:
            block_list = self.block_list
            self.clean_up_for_retry(block_list)

        n_jobs = min(len(block_list), self.max_jobs)
        # prime and run the jobs
        self.prepare_jobs(n_jobs, block_list, config)
        self.submit_jobs(n_jobs)

        # wait till jobs finish and check for job success
        self.wait_for_jobs()
        self.check_jobs(n_jobs)


class BlockEdgeFeaturesLocal(BlockEdgeFeaturesBase, LocalTask):
    """ BlockEdgeFeatures on local machine
    """
    pass


class BlockEdgeFeaturesSlurm(BlockEdgeFeaturesBase, SlurmTask):
    """ BlockEdgeFeatures on slurm cluster
    """
    pass


class BlockEdgeFeaturesLSF(BlockEdgeFeaturesBase, LSFTask):
    """ BlockEdgeFeatures on lsf cluster
    """
    pass


#
# Implementation
#


def _accumulate(input_path, input_key,
                labels_path, labels_key,
                output_path, block_list,
                graph_block_prefix, offsets):

    fu.log("accumulate features without applying filters")
    with vu.file_reader(input_path, 'r') as f:
        dtype = f[input_key].dtype
        input_dim = f[input_key].ndim
    out_prefix = os.path.join(output_path, 'blocks')
    # TODO print block success in c++ !
    if offsets is None:
        # TODO implement accumulation with filters on the fly (need halo)
        # TODO allow reduction from 4d -> 3d before accumulating boundary map features
        assert input_dim == 3
        boundary_function = ndist.extractBlockFeaturesFromBoundaryMaps_uint8 if dtype == 'uint8' else \
            ndist.extractBlockFeaturesFromBoundaryMaps_float32
        boundary_function(graph_block_prefix,
                          input_path, input_key,
                          labels_path, labels_key,
                          block_list, out_prefix)
    else:
        assert input_dim == 4
        assert filters is None
        affinity_function = ndist.extractBlockFeaturesFromAffinityMaps_uint8 if dtype == 'uint8' else \
            ndist.extractBlockFeaturesFromAffinityMaps_float32

        affinity_function(graph_block_prefix,
                          input_path, input_key,
                          labels_path, labels_key,
                          block_list, out_prefix, offsets)


def _accumulate_block(block_id, blocking,
                      ds_in, ds_labels,
                      out_prefix, graph_block_prefix,
                      filters, sigmas, halo):

    if sum(halo) > 0:
        block = blocking.getBlockWithHalo(block_id, halo)
    else:
        block = blocking.getBlock(block_id)

    input_dim = ds_in.ndim
    # TODO make choice of channels optional
    if input_dim == 4:
        bb_in = (slice(0, 3),) + bb_in

    input_ = ds_in[bb_in]
    # TODO make different dim reductions optional
    if input_dim == 4:
        input_ = np.mean(input_, axis=0)

    # load graph

    # load labels
    labels = ds_labels[bb]

    # TODO pre-smoothing ?!
    # accumulate the edge features
    edge_features = [_accumulate_filter(input_, graph, labels, bb_local) for filter_name in filters
                     for sigma in sigmas]
    edge_features = np.concatenate(edge_features, axis=1)

    # save the features

    fu.log_block_success(block_id)


def _accumulate_with_filters(input_path, input_key,
                             labels_path, labels_key,
                             output_path, graph_block_prefix,
                             block_list, block_shape,
                             filters, sigmas, halo):

    fu.log("accumulate features with applying filters:")
    # TODO print filter and sigma values
    with vu.file_reader(input_path, 'r') as f:
        ds = f[input_key]
        dtype = ds.dtype
        input_dim = ds.ndim
        shape = ds.shape
        if input_dim == 4:
            shape = shape[1:]

    out_prefix = os.path.join(output_path, 'blocks')
    blocking = nt.blocking([0, 0, 0], list(shape),
                           list(block_shape))

    with vu.file_reader(input_path) as f, vu.file_reader(labels_path) as f_l:
        ds_in = f[input_key]
        ds_labels = f_l[labels_key]
        for block_id in block_list:
            _accumulate_block(block_id, blocking,
                              ds_in, ds_labels,
                              out_prefix, graph_block_prefix,
                              filters, sigmas, halo)


def block_edge_features(job_id, config_path):

    fu.log("start processing job %i" % job_id)
    fu.log("reading config from %s" % config_path)

    # get the config
    with open(config_path, 'r') as f:
        config = json.load(f)

    block_list = config['block_list']
    input_path = config['input_path']
    input_key = config['input_key']
    labels_path = config['labels_path']
    labels_key = config['labels_key']
    output_path = config['output_path']
    block_shape = config['block_shape']
    graph_block_prefix = config['graph_block_prefix']

    # offsets for accumulation of affinity maps
    offsets = config.get('offsets', None)
    filters = config.get('filters', None)
    sigmas = config.get('sigmas', None)
    halo = config.get('halo', [0, 0, 0])

    if filters is None:
        _accumulate(input_path, input_key,
                    labels_path, labels_key,
                    output_path, block_list,
                    graph_block_prefix, offsets)
    else:
        assert offsets is None, "Filters and offsets are not supported"
        assert sigmas is not None, "Need sigma values"
        _accumulate_with_filters(input_path, input_key,
                                 labels_path, labels_key,
                                 output_path, graph_bock_prefix,
                                 block_list, block_shape,
                                 filters, sigmas, halo)

    fu.log_job_success(job_id)


if __name__ == '__main__':
    path = sys.argv[1]
    assert os.path.exists(path), path
    job_id = int(os.path.split(path)[1].split('.')[0].split('_')[-1])
    block_edge_features(job_id, path)
