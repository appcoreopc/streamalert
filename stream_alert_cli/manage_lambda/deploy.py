"""
Copyright 2017-present, Airbnb Inc.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

   http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
"""
from collections import namedtuple
import sys

from stream_alert.shared import rule_table
from stream_alert.shared.logger import get_logger
from stream_alert_cli import helpers
from stream_alert_cli.manage_lambda import package as stream_alert_packages
from stream_alert_cli.terraform.generate import terraform_generate_handler

LOGGER = get_logger(__name__)

PackageMap = namedtuple('package_attrs', ['package_class', 'targets', 'enabled'])


def deploy_handler(options, config):
    """CLI handler for deploying new versions of Lambda functions

    Args:
        options (argparse.Namespace): Parsed argparse namespace from the CLI
        config (CLIConfig): Loaded StreamAlert config

    Returns:
        bool: False if errors occurred, True otherwise
    """
    # Make sure the Terraform code is up to date
    if not terraform_generate_handler(config=config):
        return False

    functions = options.function

    if 'all' in options.function:
        functions = {
            'alert',
            'alert_merger',
            'apps',
            'athena',
            'classifier',
            'rule',
            'rule_promo',
            'threat_intel_downloader'
        }

    if not deploy(functions, config, options.clusters):
        return False

    # Update the rule table now if the rules engine is being deployed
    if 'rule' in functions:
        _update_rule_table(options, config)

    return True


def deploy(functions, config, clusters=None):
    """Deploy the functions

    Args:
        functions (set): Set of functions being deployed
        config (CLIConfig): Loaded StreamAlert config
        clusters (set=None): Optional clusters to target for this deploy

    Returns:
        bool: False if errors occurred, True otherwise
    """

    LOGGER.info('Deploying: %s', ' '.join(sorted(functions)))

    # Terraform apply only to the module which contains our lambda functions
    deploy_targets = set()
    packages = []

    for function in functions:
        package, targets = _create(function, config, clusters)
        # Continue if the package isn't enabled
        if not all([package, targets]):
            continue

        packages.append(package)
        deploy_targets.update(targets)

    # Terraform applies the new package and publishes a new version
    return helpers.tf_runner(targets=deploy_targets)


def _update_rule_table(options, config):
    """Update the rule table with any staging information

    Args:
        options (argparse.Namespace): Various options from the CLI needed for actions
        config (CLIConfig): The loaded StreamAlert config
    """
    # If rule staging is disabled, do not update the rules table
    if not config['global']['infrastructure']['rule_staging'].get('enabled', False):
        return

    # Get the rule import paths to load
    rule_import_paths = config['global']['general']['rule_locations']

    table_name = '{}_streamalert_rules'.format(config['global']['account']['prefix'])
    table = rule_table.RuleTable(table_name, *rule_import_paths)
    table.update(options.skip_rule_staging)

    if options.stage_rules or options.unstage_rules:
        # Create a dictionary of rule_name: stage=True|False
        rules = {rule_name: False for rule_name in options.unstage_rules}
        rules.update({rule_name: True for rule_name in options.stage_rules})
        for rule, stage in rules.iteritems():
            table.toggle_staged_state(rule, stage)


def _create(function_name, config, clusters=None):
    """
    Args:
        function_name: The name of the function to create and upload
        config (CLIConfig): The loaded StreamAlert config
        cluster (string): The cluster to deploy to

    Returns:
        tuple (LambdaPackage, set): The created Lambda package and the set of Terraform targets
    """
    clusters = clusters or config.clusters()

    package_mapping = {
        'alert': PackageMap(
            stream_alert_packages.AlertProcessorPackage,
            {'module.alert_processor_iam', 'module.alert_processor_lambda'},
            True
        ),
        'alert_merger': PackageMap(
            stream_alert_packages.AlertMergerPackage,
            {'module.alert_merger_iam', 'module.alert_merger_lambda'},
            True
        ),
        'apps': PackageMap(
            stream_alert_packages.AppPackage,
            {'module.app_{}_{}_{}'.format(app_info['app_name'], cluster, suffix)
             for suffix in {'lambda', 'iam'}
             for cluster, info in config['clusters'].iteritems()
             for app_info in info['modules'].get('stream_alert_apps', {}).values()
             if 'app_name' in app_info},
            True if any(info['modules'].get('stream_alert_apps')
                        for info in config['clusters'].itervalues()) else False
        ),
        'athena': PackageMap(
            stream_alert_packages.AthenaPackage,
            {'module.stream_alert_athena'},
            True
        ),
        'classifier': PackageMap(
            stream_alert_packages.ClassifierPackage,
            {'module.classifier_{}_{}'.format(cluster, suffix)
             for suffix in {'lambda', 'iam'}
             for cluster in clusters},
            True
        ),
        'rule': PackageMap(
            stream_alert_packages.RulesEnginePackage,
            {'module.rules_engine_iam', 'module.rules_engine_lambda'},
            True
        ),
        'rule_promo': PackageMap(
            stream_alert_packages.RulePromotionPackage,
            {'module.rule_promotion_iam', 'module.rule_promotion_lambda'},
            config['lambda'].get('rule_promotion_config', {}).get('enabled', False)
        ),
        'threat_intel_downloader': PackageMap(
            stream_alert_packages.ThreatIntelDownloaderPackage,
            {'module.threat_intel_downloader'},
            config['lambda'].get('threat_intel_downloader_config', False)
        )
    }

    if not package_mapping[function_name].enabled:
        return False, False

    package = package_mapping[function_name].package_class(config=config)
    if not package.create():
        return sys.exit(1)

    return package, package_mapping[function_name].targets
