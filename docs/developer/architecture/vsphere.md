# vSphere

## High-Level information

vSphere is a VMware product dedicated to managing an (usually) on-premise infrastructure. From physical machines running [VMware ESXi](https://en.wikipedia.org/wiki/VMware_ESXi) that are called ESXi Hosts, users can spin up or migrate Virtual Machines from one host to another.

### Product overview

vSphere is an integrated solution and provides an easy managing interface over concepts like data storage, or computing resource. 

### Terminology

This section details some of vSphere-specific elements. This section does not intend to be an extensive list, but rather a place for those unfamiliar with the product to have the basics required to understand how the Datadog integration works.

- vSphere - The complete suite of tools and technologies detailed in this article.
- vCenter server - The main machine which controls ESXi hosts and provides both a web UI and an API to control the vSphere environment.
- vCSA (vCenter Server Appliance) - A specific kind of vCenter where the software runs in a dedicated Linux machine (more recent). By opposition, the legacy vCenter is installed on an existing Windows Machine.
- ESXi host - The physical machine controlled by vCenter where the ESXi (bare-metal) virtualizer is installed. The host boots a minimal OS that is able to run Virtual Machines.
- VM - What anyone using vSphere actually needs in the end, instances that can run applications and code. Note: Datadog monitors both ESXi hosts and VMs and it calls them both "host" (they are in the host map).
- Attributes/tags - It is possible to add attributes and tags to any vSphere ressource, note that those two are now very similar with "attributes" being the deprecated thing to use.
- Datacenter - A set of resources grouped together. A single vCenter server can handle multiple datacenter.
- Datastore - A virtual vSphere concept to represent data storing capabilities. It can be a NFS server that ESXi hosts have read/write access to, it can be a mounted disk on the host and more. Datastores are often shared between multiple hosts. This allows Virtual Machines to be migrated from one host to another.
- Cluster - A logical grouping of computational resources, you can add multiple ESXi hosts in your cluster and then you can create VM in the cluster (and not on a specific host, vSphere will take care of placing your VM in one of the ESXi host and migrating it when needed).
- Photon OS - An open-source mimimal Linux distribution and used by both ESXi and vCSA as a base.



## The integration

### Setup

The Datadog vSphere integration runs from a single agent and pulls all the information from a single vCenter endpoint. Because the agent cannot run directly on Photon OS, it is usually require that the agent runs within a dedicated VM inside the vSphere infrastructure.



Once the agent is running, the minimal configuration (as of version 5.x) is as follows:



```yaml
init_config:
instances:
  - host: <HOST>
    username: <USERNAME>
    password: <PASSWORD>
    use_legacy_check_version: false
    empty_default_hostname: true
```

- `host` is the endpoint used to access the vSphere Client from a web browser. The host is either a FQDN or an IP, not an http url.

- `username` and `password` are the credentials to log in to vCenter.

- `use_legacy_check_version`  is a backward compatibility flag. It should always be set to false and this flag will be removed in a future version of the integration. Setting it to true tells the agent to use an older and deprecated version of the vSphere integration.

- `empty_default_hostname` is a field used by the agent directly (and not the integration). By default the agent does not allow submitting metric without attaching an explicit host tag unless this flag is set to true. The vSphere integration uses that behavior for some metrics and service checks. For example the `vsphere.vm.count` metric which gives a count of the VMs in the infra is not submitted with a host tag. This is particularly important if the agent runs inside a vSphere VM. If the `vsphere.vm.count` was submitted with a host tag, the datadog backend would attach all the other host tags to the metric, for example `vsphere_type:vm` or `vsphere_host:<NAME_OF_THE_ESX_HOST>` which makes the metric almost impossible to use.
  
  

### Concepts

#### Collection level

vSphere metrics are documented in their [documentation page](https://code.vmware.com/apis/704/vsphere/vim.PerformanceManager.html) an each metric has a defined "collection level".



That level determines the amount of data gathered by the integration and especially which metrics are available. More details [here](https://docs.vmware.com/en/VMware-vSphere/6.7/com.vmware.vsphere.vcenterhost.doc/GUID-25800DE4-68E5-41CC-82D9-8811E27924BC.html#:~:text=Each%20collection%20interval%20has%20a,referred%20to%20as%20statistics%20levels.&text=Use%20for%20long%2Dterm%20performance,device%20statistics%20are%20not%20required.).



By default only the level 1 metrics are collected but this can be increased in the integration configuration file. 



#### Realtime vs historical

- Each ESXi host collects and stores data for each metric on himself and every VM it hosts every 20 seconds. Those datapoints are stored for up to one hour and are called realtime. Note: Each metric concerns always either a VM or a ESXi hosts. Metrics that concern datastore for example are not collected in the ESXi hosts.

- Additionally, the vCenter server collects data from all the ESXi hosts and stores the datapoint with some aggregation rollup into its own database. Those datapoints are called "historical".

- Finally the vCenter server also collects metrics for other kind of resources (like Datastore, ClusterComputeResource, Datacenter...) Those datapoints are necessarily "historical".



The reason for such an important distinction is that historical metrics are **much MUCH** slower to collect than realtime metrics. The vSphere integration will always collect the "realtime" data for metrics that concern ESXi hosts and VMs. But the integration also collects metrics for Datastores, ClusterComputeResources, Datacenters and maybe other in the future.

That's why, in the context of the Datadog vSphere integration we usually simplify by considering that:

- VMs and ESXi hosts are "realtime resources". Metrics for such resources are quick and easy to get by querying vCenter that will in turn query all the ESXi hosts.

- Datastores, ClusterComputeResources and Datacenters are "historical resources" and are much slower to collect.



In order to collect all metrics (realtime and historical), it is advised to use two "check instances". One with `collection_type: realtime` and one with `collection_type: historical` . This way all metrics will be collected but because both check instances are on a different schedules, the slowness of collecting historical metrics won't affect the rate at which realtime metrics are collected.



#### vSphere tags and attributes

Similarly to how Datadog allows you to add tags to your different hosts (thins like the `os` or the `instance-type` of your machines), vSphere has "tags" and "attributes".

A lot of details can be found here: [https://docs.vmware.com/en/VMware-vSphere/7.0/com.vmware.vsphere.vcenterhost.doc/GUID-E8E854DD-AA97-4E0C-8419-CE84F93C4058.html#:~:text=Tags%20and%20attributes%20allow%20you,that%20tag%20to%20a%20category.](https://docs.vmware.com/en/VMware-vSphere/7.0/com.vmware.vsphere.vcenterhost.doc/GUID-E8E854DD-AA97-4E0C-8419-CE84F93C4058.html#:~:text=Tags%20and%20attributes%20allow%20you,that%20tag%20to%20a%20category.)



But the overall idea is that both tags and attributes are additional information that you can attach to your vSphere resources and that "tags" are newer and more featureful than "attributes".



#### Filtering

A very flexible filtering system has been implemented with the vSphere integration.

This allows fine tuned configuration so that:

- You only pay for the host and VMs you really want to monitor.







#### Instance tag



#### Performance and tuning






