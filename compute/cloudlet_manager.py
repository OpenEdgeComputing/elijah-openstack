# Elijah: Cloudlet Infrastructure for Mobile Computing
#
#   Author: Kiryong Ha <krha@cmu.edu>
#
#   Copyright (C) 2011-2014 Carnegie Mellon University
#   Licensed under the Apache License, Version 2.0 (the "License");
#   you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS,
#   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#   See the License for the specific language governing permissions and
#   limitations under the License.
#

import functools

from nova.compute import task_states
from nova.openstack.common import log as logging
from nova.objects import block_device as block_device_obj
from nova.objects import quotas as quotas_obj
from nova.compute import manager as compute_manager
from nova.virt import driver
from nova import rpc
from nova.openstack.common.gettextutils import _
from nova import exception
from nova.openstack.common import excutils
from nova import utils


LOG = logging.getLogger(__name__)

get_notifier = functools.partial(rpc.get_notifier, service='compute')


class CloudletComputeManager(compute_manager.ComputeManager):
    """Manages the running instances from creation to destruction."""
    RPC_API_VERSION = '3.34'

    def __init__(self, compute_driver=None, *args, **kwargs):
        super(CloudletComputeManager, self).__init__(*args, **kwargs)

        # make sure to load cloudlet Driver which inherit libvirt driver
        # change at /etc/nova/nova-compute.conf
        self.driver = driver.load_compute_driver(self.virtapi, compute_driver)

    @compute_manager.object_compat
    @compute_manager.wrap_exception()
    @compute_manager.reverts_task_state
    @compute_manager.wrap_instance_fault
    def cloudlet_create_base(self, context, instance, vm_name, 
            disk_meta_id, memory_meta_id, 
            diskhash_meta_id, memoryhash_meta_id):
        """Cloudlet base creation
        and terminate the instance
        """
        context = context.elevated()
        LOG.info(_("Generating cloudlet base"), instance=instance)

        self._notify_about_instance_usage(context, instance, "snapshot.start")

        def callback_update_task_state(task_state, expected_state=task_states.IMAGE_SNAPSHOT):
            instance.task_state = task_state
            instance.save(expected_task_state=expected_state)
            return instance

        self.driver.cloudlet_base(context, instance, vm_name,
                disk_meta_id, memory_meta_id,
                diskhash_meta_id, memoryhash_meta_id, callback_update_task_state)
        instance = self._instance_update(context, instance['uuid'],
                task_state=None,
                expected_task_state=task_states.IMAGE_UPLOADING)

        # notify will raise exception since instance is already deleted
        self._notify_about_instance_usage( context, instance, "snapshot.end")
        self.cloudlet_terminate_instance(context, instance)

    @compute_manager.object_compat
    @compute_manager.wrap_exception()
    @compute_manager.reverts_task_state
    @compute_manager.wrap_instance_fault
    def cloudlet_overlay_finish(self, context, instance, overlay_name, overlay_id):
        """
        Generate VM overlay with given instance, and save it as a snapshot
        """
        context = context.elevated()
        LOG.info(_("Generating VM overlay"), instance=instance)

        def callback_update_task_state(task_state, expected_state=task_states.IMAGE_SNAPSHOT):
            instance.task_state = task_state
            instance.save(expected_task_state=expected_state)
            return instance

        self.driver.create_overlay_vm(context, instance, overlay_name,
                overlay_id, callback_update_task_state)
        self.cloudlet_terminate_instance(context, instance)

    @compute_manager.object_compat
    @compute_manager.wrap_exception()
    @compute_manager.reverts_task_state
    @compute_manager.wrap_instance_fault
    def cloudlet_handoff(self, context, instance, handoff_type, dest_vm_name, residue_glance_id=None):
        """
        Perform VM handoff
        """
        context = context.elevated()
        LOG.info(_("Perform VM handoff"), instance=instance)

        def callback_update_task_state(task_state, expected_state=task_states.IMAGE_SNAPSHOT):
            instance.task_state = task_state
            instance.save(expected_task_state=expected_state)
            return instance

        self.driver.perform_vmhandoff(context, instance, handoff_type,
                                      dest_vm_name, callback_update_task_state,
                                      residue_glance_id)
        #self.cloudlet_terminate_instance(context, instance)

    # Direct calling of terminate_instance at the manager.py will cause "InstanceActionNotFound_Remote" 
    # exception at wrap_instance_event decorator since the VM is already terminated.
    # Instead, we copy-pasted terminate_instance method
    @compute_manager.wrap_exception()
    @compute_manager.reverts_task_state
    @compute_manager.wrap_instance_fault
    def cloudlet_terminate_instance(self, context, instance):
        bdms = block_device_obj.BlockDeviceMappingList.get_by_instance_uuid(
                context, instance['uuid'])

        # copy & paste from terminate_instance at manager.py
        quotas = quotas_obj.Quotas.from_reservations(context,
                                                  None,
                                                  instance=instance)

        @utils.synchronized(instance['uuid'])
        def do_terminate_instance(instance, bdms):
            try:
                self._delete_instance(context, instance, bdms, quotas)
            except exception.InstanceNotFound:
                LOG.info(_("Instance is terminate"), instance=instance)
            except Exception:
                # As we're trying to delete always go to Error if something
                # goes wrong that _delete_instance can't handle.
                with excutils.save_and_reraise_exception():
                    LOG.exception(_('Setting instance vm_state to ERROR'),
                                  instance=instance)
                    self._set_instance_error_state(context, instance)

        do_terminate_instance(instance, bdms)
