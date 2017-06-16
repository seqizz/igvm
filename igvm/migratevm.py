import logging

from igvm.exceptions import IGVMError, InconsistentAttributeError
from igvm.host import with_fabric_settings
from igvm.hypervisor import Hypervisor
from igvm.utils.preparevm import run_puppet
from igvm.utils.transaction import run_in_transaction
from igvm.vm import VM

log = logging.getLogger(__name__)


@with_fabric_settings   # NOQA: C901
@run_in_transaction
def migratevm(vm_hostname, hypervisor_hostname, newip=None, runpuppet=False,
              maintenance=False, offline=False, tx=None,
              ignore_reserved=False):
    """Migrate a VM to a new hypervisor."""
    assert tx is not None, 'tx populated by run_in_transaction'

    vm = VM(vm_hostname)
    hypervisor = Hypervisor(
        hypervisor_hostname, ignore_reserved=ignore_reserved
    )
    was_running = vm.is_running()

    # There is no point of online migration, if the VM is already shutdown.
    if not was_running:
        offline = True

    if not offline and newip:
        raise IGVMError('Online migration cannot change IP address.')

    if not offline and runpuppet:
        raise IGVMError('Online migration cannot run Puppet.')

    if not runpuppet and newip:
        raise IGVMError(
            'Changing IP requires a Puppet run, pass --runpuppet.'
        )

    # Require VM to be in sync with serveradmin
    check_attributes(vm)

    vm.check_serveradmin_config()
    vm.hypervisor.check_migration(vm, hypervisor, offline)

    if newip:
        vm._set_ip(newip)

    # Validate destination hypervisor can run the VM (needs to happen after
    # setting new IP!)
    hypervisor.check_vm(vm)

    # Setup destination Hypervisor
    hypervisor.create_vm_storage(vm, tx)

    # Commit previously changed IP address.
    if newip:
        # TODO: This commit is not rolled back.
        vm.server_obj.commit()
        tx.on_rollback('newip warning', log.info, '--newip is not rolled back')

    # The source hypervisor need to connect to the destination hypervisor
    # via SSH for the migration.  We need to ensure the source has the host
    # public key of the destination.  TODO: Get rid of this when we have
    # a usable host key distribution mechanism
    vm.hypervisor.accept_ssh_hostkey(hypervisor)

    if maintenance or offline:
        vm.set_state('maintenance', tx=tx)

    # Finally migrate the VM
    if offline and was_running:
        vm.shutdown(tx=tx)

    vm.hypervisor.migrate_vm(vm, hypervisor, offline, tx)

    if runpuppet:
        hypervisor.mount_vm_storage(vm, tx)
        run_puppet(hypervisor, vm, clear_cert=False, tx=tx)
        hypervisor.umount_vm_storage(vm)

    existing_hypervisor = vm.hypervisor
    vm.hypervisor = hypervisor

    def _reset_hypervisor():
        vm.hypervisor = existing_hypervisor
    tx.on_rollback('reset hypervisor', _reset_hypervisor)

    if offline and was_running:
        vm.start(tx=tx)
    vm.reset_state()

    # Update Serveradmin
    vm.server_obj['xen_host'] = hypervisor.server_obj['hostname']
    vm.server_obj.commit()

    # If removing the existing VM fails we shouldn't risk undoing the newly
    # migrated one.
    tx.checkpoint()

    existing_hypervisor.delete_vm(vm)


def check_attributes(vm):
    synced_attributes = vm.hypervisor.vm_sync_from_hypervisor(vm)
    for attr, value in synced_attributes.iteritems():
        if vm.server_obj[attr] != value:
            raise InconsistentAttributeError(vm, attr, value)
