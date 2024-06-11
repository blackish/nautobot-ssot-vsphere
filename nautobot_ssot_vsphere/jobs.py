#  pylint: disable=keyword-arg-before-vararg
#  pylint: disable=too-few-public-methods
#  pylint: disable=too-many-locals
#  pylint: disable=abstract-method

"""vSphere Data Target Job."""
from diffsync.enum import DiffSyncFlags
from diffsync.exceptions import ObjectNotCreated
from django.templatetags.static import static
from django.urls import reverse
from nautobot.extras.jobs import BooleanVar, ScriptVariable
from nautobot.core.celery import register_jobs
from nautobot.core.forms.fields import DynamicModelChoiceField
from nautobot.virtualization.models import Cluster
from nautobot_ssot.jobs.base import DataMapping, DataSource

from nautobot_ssot_vsphere.diffsync import defaults
from nautobot_ssot_vsphere.diffsync.adapters import NautobotDiffSync, VsphereDiffSync
from nautobot_ssot_vsphere.utilities import VsphereClient

name = "SSoT - Virtualization"  # pylint: disable=invalid-name


class OptionalObjectVar(ScriptVariable):
    """Custom implementation of an Optional ObjectVar.

    An object primary key is returned and accessible in job kwargs.
    """

    form_field = DynamicModelChoiceField

    def __init__(
        self,
        model=None,
        display_field="display",
        query_params=None,
        null_option=None,
        *args,
        **kwargs,
    ):
        """Init."""
        super().__init__(*args, **kwargs)

        if model is not None:
            self.field_attrs["queryset"] = model.objects.all()
        else:
            raise TypeError("ObjectVar must specify a model")

        self.field_attrs.update(
            {
                "display_field": display_field,
                "query_params": query_params,
                "null_option": null_option,
            }
        )


# pylint:disable=too-few-public-methods
class VspherecDataSource(DataSource):
    """Job syncing data from vSphere to Nautobot."""

    debug = BooleanVar(description="Enable for more verbose debug logging")
    sync_vsphere_tagged_only = BooleanVar(
        default=False,
        label="Sync Tagged Only",
        description="Only sync objects that have the 'ssot-synced-from-vsphere' tag.",
    )
    if defaults.DEFAULT_USE_CLUSTERS:
        cluster_filter = OptionalObjectVar(
            description="Only sync Nautobot records belonging to a single Cluster.",
            model=Cluster,
            required=False,
        )

    class Meta:
        """Metadata about this Job."""

        name = "VMWare vSphere ⟹ Nautobot"
        data_source = "VMWare vSphere"
        data_source_icon = static("nautobot_ssot_vsphere/vmware.png")
        description = "Sync data from VMWare vSphere into Nautobot."
        field_order = (
            "debug",
            "sync_vsphere_tagged_only",
            "dry_run",
        )

    @classmethod
    def data_mappings(cls):
        """List describing the data mappings involved in this DataSource."""
        return (
            DataMapping("Data Center", None, "ClusterGroup", reverse("virtualization:clustergroup_list")),
            DataMapping("Cluster", None, "Cluster", reverse("virtualization:cluster_list")),
            DataMapping("Virtual Machine", None, "Virtual Machine", reverse("virtualization:virtualmachine_list")),
            DataMapping("VM Interface", None, "VMInterface", reverse("virtualization:vminterface_list")),
            DataMapping("IP Addresses", None, "IP Addresses", reverse("ipam:ipaddress_list")),
        )

    @classmethod
    def config_information(cls):
        """Configuration of this DataSource."""
        return {
            "vSphere URI": defaults.VSPHERE_URI,
            "vSphere Username": defaults.VSPHERE_USERNAME,
            "vSphere Verify SSL": "False" if not defaults.VSPHERE_VERIFY_SSL else "True",
            "vSphere Cluster Type": defaults.DEFAULT_VSPHERE_TYPE,
            "Enforce ClusterGroup as Top Level": "False" if not defaults.ENFORCE_CLUSTER_GROUP_TOP_LEVEL else "True",
            "Default Virtual Machine Status Map": defaults.DEFAULT_VM_STATUS_MAP,
            "Default VMInterface Enabled Map": defaults.VSPHERE_VM_INTERFACE_MAP,
            "Default IP Status Map": defaults.DEFAULT_IP_STATUS_MAP,
            "Primary IP Assignment": defaults.PRIMARY_IP_SORT_BY,
            "Default Use Clusers": defaults.DEFAULT_USE_CLUSTERS,
            "Default Cluster Name": defaults.DEFAULT_CLUSTER_NAME,
        }

    def log_debug(self, message):
        """Conditionally log a debug message."""
        if self.debug:
            self.logger.debug(message)

    def run(  # pylint: disable=arguments-differ, too-many-arguments
       self, dryrun, memory_profiling, debug, sync_vsphere_tagged_only, cluster_filter, *args, **kwargs
    ):
        """Perform data synchronization."""
        self.sync_vsphere_tagged_only = sync_vsphere_tagged_only
        self.cluster_filter = cluster_filter
        self.debug = debug
        self.dryrun = dryrun
        self.memory_profiling = memory_profiling
        super().run(dryrun=self.dryrun, memory_profiling=self.memory_profiling, *args, **kwargs)

    def sync_data(self, memory_profiling):
        """Sync a device data from vSphere into Nautobot."""
        dry_run = self.dryrun
        tagged_only = self.sync_vsphere_tagged_only
        debug_mode = self.debug

        if defaults.DEFAULT_USE_CLUSTERS:
            cluster_filter_object = (
                Cluster.objects.get(pk=self.cluster_filter) if self.cluster_filter else None
            )
        else:
            self.logger.info(msg="`DEFAULT_USE_CLUSTERS` is set to `False`")
            if defaults.ENFORCE_CLUSTER_GROUP_TOP_LEVEL:
                self.logger.failure(msg="Cannot `ENFORCE_CLUSTER_GROUP_TOP_LEVEL` and disable `DEFAULT_USE_CLUSTERS`")
                self.logger.info(
                    msg="Set `ENFORCE_CLUSTER_GROUP_TOP_LEVEL` to `False` or `DEFAULT_USE_CLUSTERS` to `True`"
                )
            cluster_filter_object = None

        options = f"`Debug`: {debug_mode}, `Dry Run`: {dry_run}, `Sync Tagged Only`: {tagged_only}, `Cluster Filter`: {cluster_filter_object}"  # NOQA
        self.logger.info(msg=f"Starting job with the following options: {options}")
        vsphere_source = VsphereDiffSync(
            job=self, sync=self.sync, client=VsphereClient(), cluster_filter=cluster_filter_object
        )

        self.logger.info(msg="Loading current data from vSphere...")
        vsphere_source.load()

        dest = NautobotDiffSync(
            job=self,
            sync=self.sync,
            sync_vsphere_tagged_only=tagged_only,
            cluster_filter=cluster_filter_object,
        )

        self.logger.info(msg="Loading current data from Nautobot...")
        dest.load()

        self.logger.info(msg="Calculating diffs...")
        flags = DiffSyncFlags.CONTINUE_ON_FAILURE

        diff = dest.diff_from(vsphere_source, flags=flags)
        self.log_debug(message=f"Diff: {diff.dict()}")

        self.sync.diff = diff.dict()
        self.sync.save()
        create = diff.summary().get("create")
        update = diff.summary().get("update")
        delete = diff.summary().get("delete")
        no_change = diff.summary().get("no-change")
        self.logger.info(
            msg=f"DiffSync Summary: Create: {create}, Update: {update}, Delete: {delete}, No Change: {no_change}"
        )
        if not dry_run:
            self.logger.info(msg="Syncing from vSphere to Nautobot")
            try:
                dest.sync_from(vsphere_source, flags=flags)
            except ObjectNotCreated as err:
                self.logger.warning(f"Unable to create object. {err}")
            # except Exception as err:  # Keep it general, as final resort
            #     self.log_warning(f"Error occured. {err}")

        self.logger.info(msg="Sync complete.")


jobs = [VspherecDataSource]
register_jobs(*jobs)
