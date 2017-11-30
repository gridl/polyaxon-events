# -*- coding: utf-8 -*-
from __future__ import absolute_import, division, print_function

import json
import logging
import os
import time

from kubernetes import watch
from kubernetes.client.rest import ApiException

from polyaxon_k8s.manager import K8SManager
from polyaxon_k8s.constants import PodConditions, PodLifeCycle, JobLifeCycle

from polyaxon_events import settings
from polyaxon_events.job_containers import JobContainers
from polyaxon_events.publisher import Publisher
from polyaxon_events.utils import datetime_handler

logger = logging.getLogger('polyaxon.events')


def get_pod_status(event):
    # For terminated pods that failed and successfully terminated pods
    if event.status.phase == PodLifeCycle.FAILED:
        return JobLifeCycle.FAILED

    if event.status.phase == PodLifeCycle.SUCCEEDED:
        return JobLifeCycle.SUCCEEDED

    if event.metadata.deletion_timestamp:
        return JobLifeCycle.DELETED

    if not event.status.conditions:
        return JobLifeCycle.UNKNOWN

    conditions = {c.type: c.status for c in event.status.conditions}

    if not (conditions[PodConditions.SCHEDULED] or conditions[PodConditions.READY]):
        return JobLifeCycle.BUILDING

    # Unknown?
    return PodLifeCycle.UNKNOWN


def update_job_containers(event, job_container_name):
    if event.status.container_statuses is None:
        return

    def get_container_id(container_id):
        if container_id.startswith('docker://'):
            return container_id[len('docker://'):]
        return container_id

    for container_status in event.status.container_statuses:
        if container_status.name != job_container_name:
            continue

        container_id = container_status.container_id
        if container_id:
            container_id = get_container_id(container_id)
            job_id = event.metadata.labels['task']
            if container_status.state.running is not None:
                logger.info('Monitoring (container_id, job_id): ({}, {})'.format(container_id,
                                                                                 job_id))
                JobContainers.monitor(container_id, job_id)


def parse_event(raw_event, experiment_type_label, job_container_name):
    event_type = raw_event['type']
    event = raw_event['object']
    labels = event.metadata.labels
    if labels['type'] != experiment_type_label:  # 2 type: core and experiment
        return

    update_job_containers(event, job_container_name)
    pod_phase = event.status.phase
    deletion_timestamp = event.metadata.deletion_timestamp
    pod_conditions = event.status.conditions
    container_statuses = event.status.container_statuses
    container_statuses_by_name = {
        container_status.name: {
            'ready': container_status.ready,
            'state': container_status.state.to_dict(),
        } for container_status in container_statuses
    }

    return {
        'event_type': event_type,
        'labels': labels,
        'pod_phase': pod_phase,
        'pod_status': get_pod_status(event),
        'deletion_timestamp': deletion_timestamp,
        'pod_conditions': [pod_condition.to_dict() for pod_condition in pod_conditions],
        'container_statuses': container_statuses_by_name
    }


def run(k8s_manager,
        publisher,
        experiment_type_label,
        job_container_name,
        label_selector=None):
    w = watch.Watch()

    for event in w.stream(k8s_manager.k8s_api.list_namespaced_pod,
                          namespace=k8s_manager.namespace,
                          label_selector=label_selector):
        logger.debug("event: %s" % event)

        parsed_event = parse_event(event, experiment_type_label, job_container_name)

        if parsed_event:
            parsed_event = json.dumps(parsed_event, default=datetime_handler)
            logger.info("Publishing event: {}".format(parsed_event))
            publisher.publish(parsed_event)


def main():
    k8s_manager = K8SManager(namespace=settings.NAMESPACE, in_cluster=True)
    publisher = Publisher(os.environ['POLYAXON_ROUTING_KEYS_EVENTS_JOB_STATUSES'])
    while True:
        try:
            role_label = os.environ['POLYAXON_ROLE_LABELS_WORKER']
            type_label = os.environ['POLYAXON_TYPE_LABELS_EXPERIMENT']
            label_selector = 'role={},type={}'.format(role_label, type_label)
            run(k8s_manager,
                publisher,
                job_container_name=os.environ['POLYAXON_JOB_CONTAINER_NAME'],
                experiment_type_label=type_label,
                label_selector=label_selector)
        except ApiException as e:
            logger.error(
                "Exception when calling CoreV1Api->list_namespaced_pod: %s\n" % e)
            time.sleep(settings.LOG_SLEEP_INTERVAL)
        except Exception as e:
            logger.exception("Unhandled exception occurred %s\n" % e)


if __name__ == '__main__':
    main()
