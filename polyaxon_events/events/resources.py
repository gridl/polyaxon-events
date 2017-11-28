# -*- coding: utf-8 -*-
from __future__ import absolute_import, division, print_function

import json
import logging

import os
import re
import time

import docker
import polyaxon_gpustat
from docker.errors import NotFound
from polyaxon_k8s.constants import ContainerStatuses

from polyaxon_events import settings
from polyaxon_events.job_containers import JobContainers
from polyaxon_events.publisher import Publisher
from polyaxon_events.utils import datetime_handler

logger = logging.getLogger('polyaxon.resources')
docker_client = docker.from_env(version="auto", timeout=10)

def get_gpu_resources():
    if not polyaxon_gpustat.has_gpu_nvidia:
        return

    try:
        return polyaxon_gpustat.query()
    except:
        polyaxon_gpustat.has_gpu_nvidia = False


def get_container_gpu_indices(container):
    gpus = []
    devices = container.attrs['HostConfig']['Devices']
    for dev in devices:
        match = re.match(r'/dev/nvidia(?P<index>[0-9]+)', dev['PathOnHost'])
        if match:
            gpus.append(match.group('index'))
    return gpus


def get_container(containers, container_id):
    if container_id in containers:
        return containers[container_id]

    try:
        container = docker_client.containers.get(container_id)
    except NotFound:
        logger.info("container `{}` was not found".format(container_id))
        return None

    if container.status != ContainerStatuses.RUNNING:
        return None

    containers[container_id] = container
    return container


def get_container_resources(container, gpu_resources):
    # Check if the container is running
    if container.status != ContainerStatuses.RUNNING:
        logger.info("`{}` container is not running".format(container.name))
        JobContainers.remove_container(container.id)
        return

    job_id = JobContainers.get_job(container.id)
    if not job_id:
        logger.info("`{}` container is not recognised".format(container.name))
        return

    logger.info("Streaming resources for container {} in job `{}` ".format(container.id, job_id))

    stats = container.stats(decode=True, stream=False)
    precpu_stats = stats['precpu_stats']
    cpu_stats = stats['cpu_stats']

    pre_total_usage = float(precpu_stats['cpu_usage']['total_usage'])
    total_usage = float(cpu_stats['cpu_usage']['total_usage'])
    delta_total_usage = total_usage - pre_total_usage

    pre_system_cpu_usage = float(precpu_stats['system_cpu_usage'])
    system_cpu_usage = float(cpu_stats['system_cpu_usage'])
    delta_system_cpu_usage = system_cpu_usage - pre_system_cpu_usage

    percpu_usage = cpu_stats['cpu_usage']['percpu_usage']
    num_cpu_cores = len(percpu_usage)
    cpu_percentage = 0.
    percpu_percentage = [0.] * num_cpu_cores
    if delta_total_usage > 0 and delta_system_cpu_usage > 0:
        cpu_percentage = (delta_total_usage / delta_system_cpu_usage) * num_cpu_cores * 100.0
        percpu_percentage = [cpu_usage / total_usage * cpu_percentage for cpu_usage in percpu_usage]

    memory_used = int(stats['memory_stats']['usage'])
    memory_limit = int(stats['memory_stats']['limit'])

    container_gpu_resources = None
    if gpu_resources:
        gpu_indices = get_container_gpu_indices(container)
        container_gpu_resources = [gpu_resources[gpu_indice] for gpu_indice in gpu_indices]

    return {
        'job_id': job_id,
        'container_id': container.id,
        'cpu_percentage': cpu_percentage,
        'percpu_percentage': percpu_percentage,
        'memory_used': memory_used,
        'memory_limit': memory_limit,
        'gpu_resources': container_gpu_resources
    }


def run(publisher, containers):
    container_ids = JobContainers.get_containers()
    gpu_resources = get_gpu_resources()
    if gpu_resources:
        gpu_resources = {gpu_resource['index']: gpu_resource for gpu_resource in gpu_resources}
    for container_id in container_ids:
        container = get_container(containers, container_id)
        if not container:
            JobContainers.remove_container(container_id)
        c_resources = get_container_resources(containers[container_id], gpu_resources)
        if c_resources:
            logger.info('{}'.format(c_resources))
            publisher.publish(json.dumps(c_resources, default=datetime_handler))


def main():
    publisher = Publisher(os.environ['POLYAXON_ROUTING_KEYS_EVENTS_RESOURCES'])
    containers = {}
    while True:
        try:
            run(publisher, containers)
        except Exception as e:
            logger.exception("Unhandled exception occurred %s\n" % e)

        time.sleep(settings.LOG_SLEEP_INTERVAL)


if __name__ == '__main__':
    main()
