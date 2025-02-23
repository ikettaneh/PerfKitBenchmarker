# Copyright 2022 PerfKitBenchmarker Authors. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Class to represent a GCE Windows Virtual Machine object."""

import json
from typing import Any, Dict, Tuple

from absl import flags
from perfkitbenchmarker import errors
from perfkitbenchmarker import os_types
from perfkitbenchmarker import vm_util
from perfkitbenchmarker import windows_virtual_machine
from perfkitbenchmarker.providers.gcp import gce_virtual_machine
from perfkitbenchmarker.providers.gcp import gcs
from perfkitbenchmarker.providers.gcp import util

_WINDOWS_SHUTDOWN_SCRIPT_PS1 = 'Write-Host | gsutil cp - {preempt_marker}'

_METADATA_PREEMPT_CMD_WIN = (f'Invoke-RestMethod -Uri'
                             f' {gce_virtual_machine.METADATA_PREEMPT_URI} '
                             '-Headers @{"Metadata-Flavor"="Google"}')

FLAGS = flags.FLAGS

BAT_SCRIPT = """
'
:WAIT
echo waiting for MSSQLSERVER
sc start MSSQLSERVER
ping 127.0.0.1 -t 1 > NUL
for /f "tokens=4" %%s in ('sc query MSSQLSERVER ^| find "STATE"') do if NOT "%%s"=="RUNNING" goto WAIT
echo MSSQLSERVER is now running!
sqlcmd.exe -Q "CREATE LOGIN [%COMPUTERNAME%\\perfkit] from windows;"
sqlcmd.exe -Q "ALTER SERVER ROLE [sysadmin] ADD MEMBER [%COMPUTERNAME%\\perfkit]" '
"""


class GceUnexpectedWindowsAdapterOutputError(Exception):
  """Raised when querying the status of a windows adapter failed."""


class GceDriverDoesntSupportFeatureError(Exception):
  """Raised if there is an attempt to set a feature not supported."""


class WindowsGceVirtualMachine(gce_virtual_machine.GceVirtualMachine,
                               windows_virtual_machine.BaseWindowsMixin):
  """Class supporting Windows GCE virtual machines."""
  DEFAULT_IMAGE_FAMILY = {
      os_types.WINDOWS2012_CORE: 'windows-2012-r2-core',
      os_types.WINDOWS2016_CORE: 'windows-2016-core',
      os_types.WINDOWS2019_CORE: 'windows-2019-core',
      os_types.WINDOWS2022_CORE: 'windows-2022-core',
      os_types.WINDOWS2012_DESKTOP: 'windows-2012-r2',
      os_types.WINDOWS2016_DESKTOP: 'windows-2016',
      os_types.WINDOWS2019_DESKTOP: 'windows-2019',
      os_types.WINDOWS2022_DESKTOP: 'windows-2022',
  }

  GVNIC_DISABLED_OS_TYPES = [
      os_types.WINDOWS2012_CORE, os_types.WINDOWS2012_DESKTOP
  ]

  NVME_START_INDEX = 0
  OS_TYPE = os_types.WINDOWS_CORE_OS_TYPES + os_types.WINDOWS_DESKOP_OS_TYPES

  def __init__(self, vm_spec):
    """Initialize a Windows GCE virtual machine.

    Args:
      vm_spec: virtual_machine.BaseVmSpec object of the vm.
    """
    super(WindowsGceVirtualMachine, self).__init__(vm_spec)
    self.boot_metadata[
        'windows-startup-script-ps1'] = windows_virtual_machine.STARTUP_SCRIPT

  def _GenerateResetPasswordCommand(self):
    """Generates a command to reset a VM user's password.

    Returns:
      GcloudCommand. gcloud command to issue in order to reset the VM user's
      password.
    """
    cmd = util.GcloudCommand(self, 'compute', 'reset-windows-password',
                             self.name)
    cmd.flags['user'] = self.user_name
    return cmd

  def _PostCreate(self):
    super(WindowsGceVirtualMachine, self)._PostCreate()
    reset_password_cmd = self._GenerateResetPasswordCommand()
    stdout, _ = reset_password_cmd.IssueRetryable()
    response = json.loads(stdout)
    self.password = response['password']

  def _PreemptibleMetadataKeyValue(self) -> Tuple[str, str]:
    """See base class."""
    return 'windows-shutdown-script-ps1', _WINDOWS_SHUTDOWN_SCRIPT_PS1.format(
        preempt_marker=self.preempt_marker)

  @vm_util.Retry(
      max_retries=10,
      retryable_exceptions=(GceUnexpectedWindowsAdapterOutputError,
                            errors.VirtualMachine.RemoteCommandError))
  def GetResourceMetadata(self) -> Dict[str, Any]:
    """Returns a dict containing metadata about the VM.

    Returns:
      dict mapping metadata key to value.
    """
    result = super(WindowsGceVirtualMachine, self).GetResourceMetadata()
    result['disable_rss'] = self.disable_rss
    return result

  def DisableRSS(self):
    """Disables RSS on the GCE VM.

    Raises:
      GceDriverDoesntSupportFeatureError: If RSS is not supported.
      GceUnexpectedWindowsAdapterOutputError: If querying the RSS state
        returns unexpected output.
    """
    # First ensure that the driver supports interrupt moderation
    net_adapters, _ = self.RemoteCommand('Get-NetAdapter')
    if 'Red Hat VirtIO Ethernet Adapter' not in net_adapters:
      raise GceDriverDoesntSupportFeatureError(
          'Driver not tested with RSS disabled in PKB.')

    command = 'netsh int tcp set global rss=disabled'
    self.RemoteCommand(command)
    try:
      self.RemoteCommand('Restart-NetAdapter -Name "Ethernet"')
    except IOError:
      # Restarting the network adapter will always fail because
      # the winrm connection used to issue the command will be
      # broken.
      pass

    # Verify the setting went through
    stdout, _ = self.RemoteCommand('netsh int tcp show global')
    if 'Receive-Side Scaling State          : enabled' in stdout:
      raise GceUnexpectedWindowsAdapterOutputError('RSS failed to disable.')

  def _AcquireWritePermissionsLinux(self):
    gcs.GoogleCloudStorageService.AcquireWritePermissionsWindows(self)

  def SupportGVNIC(self) -> bool:
    return self.OS_TYPE not in self.GVNIC_DISABLED_OS_TYPES

  def GetDefaultImageFamily(self) -> str:
    return self.DEFAULT_IMAGE_FAMILY[self.OS_TYPE]

  def GetDefaultImageProject(self) -> str:
    if self.OS_TYPE in os_types.WINDOWS_SQLSERVER_OS_TYPES:
      return 'windows-sql-cloud'
    return 'windows-cloud'

  @property
  def _MetadataPreemptCmd(self) -> str:
    return _METADATA_PREEMPT_CMD_WIN


class WindowsGceSqlServerVirtualMachine(WindowsGceVirtualMachine):
  """Class supporting Windows GCE sql server virtual machines."""
  DEFAULT_IMAGE_FAMILY = {
      os_types.WINDOWS2019_SQLSERVER_2017_STANDARD: 'sql-std-2017-win-2019',
      os_types.WINDOWS2019_SQLSERVER_2017_ENTERPRISE: 'sql-ent-2017-win-2019',
      os_types.WINDOWS2019_SQLSERVER_2019_STANDARD: 'sql-std-2019-win-2019',
      os_types.WINDOWS2019_SQLSERVER_2019_ENTERPRISE: 'sql-ent-2019-win-2019',
      os_types.WINDOWS2022_SQLSERVER_2019_STANDARD: 'sql-std-2019-win-2022',
      os_types.WINDOWS2022_SQLSERVER_2019_ENTERPRISE: 'sql-ent-2019-win-2022',
  }

  OS_TYPE = os_types.WINDOWS_SQLSERVER_OS_TYPES

  def __init__(self, vm_spec):
    super().__init__(vm_spec)
    self.boot_metadata['windows-startup-script-bat'] = BAT_SCRIPT
