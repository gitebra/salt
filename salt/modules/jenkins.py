# -*- coding: utf-8 -*-
'''
Module for controlling Jenkins

.. versionadded:: Boron

:configuration: This module can be used by either passing an api key and version
    directly or by specifying both in a configuration profile in the salt
    master/minion config.

    For example:

    .. code-block:: yaml

        jenkins:
          api_key: peWcBiMOS9HrZG15peWcBiMOS9HrZG15
'''

# Import Python libs
from __future__ import absolute_import
import logging

try:
    import jenkins
    HAS_JENKINS = True
except ImportError:
    HAS_JENKINS = False

import salt.utils

# Import 3rd-party libs
# pylint: disable=import-error,no-name-in-module,redefined-builtin
from salt.exceptions import SaltInvocationError
# pylint: enable=import-error,no-name-in-module

log = logging.getLogger(__name__)

__virtualname__ = 'jenkins'


def __virtual__():
    '''
    Return virtual name of the module.

    :return: The virtual name of the module.
    '''
    if HAS_JENKINS:
        return __virtualname__
    return (False, 'The jenkins execution module cannot be loaded: '
            'python jenkins library is not installed.')


def _connect():
    '''
    Return server object used to interact with Jenkins.

    :return: server object used to interact with Jenkins
    '''
    jenkins_url = __salt__['config.get']('jenkins.url') or \
        __salt__['config.get']('jenkins:url') or \
        __salt__['pillar.get']('jenkins.url')

    jenkins_user = __salt__['config.get']('jenkins.user') or \
        __salt__['config.get']('jenkins:user') or \
        __salt__['pillar.get']('jenkins.user')

    jenkins_password = __salt__['config.get']('jenkins.password') or \
        __salt__['config.get']('jenkins:password') or \
        __salt__['pillar.get']('jenkins.password')

    if not jenkins_url:
        raise SaltInvocationError('No Jenkins URL found.')

    if not jenkins_user:
        raise SaltInvocationError('No Jenkins User found.')

    if not jenkins_password:
        raise SaltInvocationError('No Jenkins Password or API token found.')

    return jenkins.Jenkins(jenkins_url,
                           username=jenkins_user,
                           password=jenkins_password)


def get_version():
    '''
    Return version of Jenkins

    :return: The version of Jenkins

    CLI Example:

    .. code-block:: bash

        salt '*' jenkins.get_version
    '''

    server = _connect()

    version = server.get_version()
    if version:
        return version
    return False


def get_jobs():
    '''
    Return the currently configured jobs.

    :return: The currently configured jobs.

    CLI Example:

    .. code-block:: bash

        salt '*' jenkins.get_jobs
    '''

    server = _connect()

    jobs = server.get_jobs()
    if jobs:
        return jobs
    return {}


def job_exists(name=None):
    '''
    Check whether the job exists in configured Jenkins jobs.

    :param name: The name of the job is check if it exists.
    :return: True if job exists, False if job does not exist.

    CLI Example:

    .. code-block:: bash

        salt '*' jenkins.job_exists jobname

    '''
    if not name:
        raise SaltInvocationError('Required parameter `name` is missing.')

    server = _connect()
    if server.job_exists(name):
        return True
    else:
        return False


def get_job_info(name=None):
    '''
    Return information about the Jenkins job.

    :param name: The name of the job is check if it exists.
    :return: Information about the Jenkins job.

    CLI Example:

    .. code-block:: bash

        salt '*' jenkins.get_job_info jobname

    '''
    if not name:
        raise SaltInvocationError('Required parameter `name` is missing.')

    server = _connect()

    if not job_exists(name):
        raise SaltInvocationError('Job `{0}` does not exist.'.format(name))

    job_info = server.get_job_info(name)
    if job_info:
        return job_info
    return False


def build_job(name=None, parameters=None):
    '''
    Initiate a build for the provided job.

    :param name: The name of the job is check if it exists.
    :param parameters: Parameters to send to the job.
    :return: True is successful, otherwise raise an exception.

    CLI Example:

    .. code-block:: bash

        salt '*' jenkins.build_job jobname

    '''
    if not name:
        raise SaltInvocationError('Required parameter `name` is missing.')

    server = _connect()

    if not job_exists(name):
        raise SaltInvocationError('Job `{0}` does not exist.'.format(name))

    try:
        server.build_job(name, parameters)
    except jenkins.JenkinsException as err:
        raise SaltInvocationError('Something went wrong {0}.'.format(err))
    return True


def create_job(name=None,
               config_xml=None,
               saltenv='base'):
    '''
    Return the configuration file.

    :param name: The name of the job is check if it exists.
    :param config_xml: The configuration file to use to create the job.
    :param saltenv: The environment to look for the file in.
    :return: The configuration file used for the job.

    CLI Example:

    .. code-block:: bash

        salt '*' jenkins.create_job jobname

        salt '*' jenkins.create_job jobname config_xml='salt://jenkins/config.xml'

    '''
    if not name:
        raise SaltInvocationError('Required parameter `name` is missing.')

    if job_exists(name):
        raise SaltInvocationError('Job `{0}` already exists.'.format(name))

    if not config_xml:
        config_xml = jenkins.EMPTY_CONFIG_XML
    else:
        config_xml_file = __salt__['cp.cache_file'](config_xml, saltenv)

        with salt.utils.fopen(config_xml_file) as _fp:
            config_xml = _fp.read()

    server = _connect()
    try:
        server.create_job(name, config_xml)
    except jenkins.JenkinsException as err:
        raise SaltInvocationError('Something went wrong {0}.'.format(err))
    return config_xml


def update_job(name=None,
               config_xml=None,
               saltenv='base'):
    '''
    Return the updated configuration file.

    :param name: The name of the job is check if it exists.
    :param config_xml: The configuration file to use to create the job.
    :param saltenv: The environment to look for the file in.
    :return: The configuration file used for the job.

    CLI Example:

    .. code-block:: bash

        salt '*' jenkins.update_job jobname

        salt '*' jenkins.update_job jobname config_xml='salt://jenkins/config.xml'

    '''
    if not name:
        raise SaltInvocationError('Required parameter `name` is missing.')

    if not config_xml:
        config_xml = jenkins.EMPTY_CONFIG_XML
    else:
        config_xml_file = __salt__['cp.cache_file'](config_xml, saltenv)

        with salt.utils.fopen(config_xml_file) as _fp:
            config_xml = _fp.read()

    server = _connect()
    try:
        server.reconfig_job(name, config_xml)
    except jenkins.JenkinsException as err:
        raise SaltInvocationError('Something went wrong {0}.'.format(err))
    return config_xml


def delete_job(name=None):
    '''
    Return true is job is deleted successfully.

    :param name: The name of the job to delete.
    :return: Return true if job is deleted successfully.

    CLI Example:

    .. code-block:: bash

        salt '*' jenkins.delete_job jobname

    '''
    if not name:
        raise SaltInvocationError('Required parameter `name` is missing.')

    server = _connect()

    if not job_exists(name):
        raise SaltInvocationError('Job `{0}` does not exists.'.format(name))

    try:
        server.delete_job(name)
    except jenkins.JenkinsException as err:
        raise SaltInvocationError('Something went wrong {0}.'.format(err))
    return True


def enable_job(name=None):
    '''
    Return true is job is enabled successfully.

    :param name: The name of the job to enable.
    :return: Return true if job is enabled successfully.

    CLI Example:

    .. code-block:: bash

        salt '*' jenkins.enable_job jobname

    '''
    if not name:
        raise SaltInvocationError('Required parameter `name` is missing.')

    server = _connect()

    if not job_exists(name):
        raise SaltInvocationError('Job `{0}` does not exists.'.format(name))

    try:
        server.enable_job(name)
    except jenkins.JenkinsException as err:
        raise SaltInvocationError('Something went wrong {0}.'.format(err))
    return True


def disable_job(name=None):
    '''
    Return true is job is disabled successfully.

    :param name: The name of the job to disable.
    :return: Return true if job is disabled successfully.

    CLI Example:

    .. code-block:: bash

        salt '*' jenkins.disable_job jobname

    '''

    if not name:
        raise SaltInvocationError('Required parameter `name` is missing.')

    server = _connect()

    if not job_exists(name):
        raise SaltInvocationError('Job `{0}` does not exists.'.format(name))

    try:
        server.disable_job(name)
    except jenkins.JenkinsException as err:
        raise SaltInvocationError('Something went wrong {0}.'.format(err))
    return True


def job_status(name=None):
    '''
    Return the current status, enabled or disabled, of the job.

    :param name: The name of the job to return status for
    :return: Return true if enabled or false if disabled.

    CLI Example:

    .. code-block:: bash

        salt '*' jenkins.job_status jobname

    '''

    if not name:
        raise SaltInvocationError('Required parameter `name` is missing.')

    server = _connect()

    if not job_exists(name):
        raise SaltInvocationError('Job `{0}` does not exists.'.format(name))

    return server.get_job_info('empty')['buildable']


def get_job_config(name=None):
    '''
    Return the current job configuration for the provided job.

    :param name: The name of the job to return the configuration for.
    :return: The configuration for the job specified.

    CLI Example:

    .. code-block:: bash

        salt '*' jenkins.get_job_config jobname

    '''

    if not name:
        raise SaltInvocationError('Required parameter `name` is missing.')

    server = _connect()

    if not job_exists(name):
        raise SaltInvocationError('Job `{0}` does not exists.'.format(name))

    job_info = server.get_job_config(name)
    return job_info
