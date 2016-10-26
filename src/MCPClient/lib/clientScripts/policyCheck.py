#!/usr/bin/env python2
from __future__ import print_function
import json
import os
import shutil
import sys

from custom_handlers import get_script_logger

import django
django.setup()
from fpr.models import FPRule, FormatVersion
from main.models import File, SIP

from executeOrRunSubProcess import executeOrRun
import databaseFunctions
from dicts import replace_string_values

# Note that linkTaskManagerFiles.py will take the highest exit code it has seen
# from all tasks and will use that as the exit code of the job as a whole.
SUCCESS_CODE = 0
NOT_APPLICABLE_CODE = 0
FAIL_CODE = 1


class PolicyChecker:
    """Checks whether a given file conforms to all of the MediaConch policies
    that the system is configured to run against that type of file, given the
    file's format and its purpose, i.e., whether it is intended for access or
    preservation. Usage involves initializing on a file and then calling the
    ``check`` method.
    """

    def __init__(self, file_path, file_uuid, sip_uuid, shared_path):
        self.file_path = file_path
        self.file_uuid = file_uuid
        self.sip_uuid = sip_uuid
        self.shared_path = shared_path
        self.policies_dir = self.get_policies_dir()
        self.is_manually_normalized_access_derivative = \
            self.get_is_manually_normalized_access_derivative()
        self._sip_logs_dir = None
        self._sip_policies_dir = None

    def get_policies_dir(self):
        return os.path.join(self.shared_path, 'sharedMicroServiceTasksConfigs',
                            'policies')

    def get_is_manually_normalized_access_derivative(self):
        """Manually normalized access derivatives are never given UUIDs.
        Therefore, we need this heuristic for determining if that is what we
        are dealing with. TODO/QUESTION: will this return false positives?
        """
        if (self.file_uuid == 'None' and
                os.path.split(self.file_path)[0].endswith('/DIP/objects')):
            return True
        return False

    def check(self):
        """Check the passed-in file against any policy-check FPR commands that
        are applicable. If any fail, return a non-zero exit code; otherwise
        return ``0``.
        """
        if not self.is_manually_normalized_access_derivative:
            try:
                self.file_model = File.objects.get(uuid=self.file_uuid)
            except File.DoesNotExist:
                print('Not performing a policy check because there is no file'
                      ' with UUID {}.'.format(self.file_uuid))
                return NOT_APPLICABLE_CODE
        if not self.we_check_this_type_of_file():
            return NOT_APPLICABLE_CODE
        rules = self._get_rules()
        if not rules:
            print('Not performing a policy check because there are no relevant'
                  ' FPR rules')
            return NOT_APPLICABLE_CODE
        rule_outputs = []
        for rule in rules:
            rule_outputs.append(self._execute_rule_command(rule))
        if 'failed' in rule_outputs:
            return FAIL_CODE
        else:
            return SUCCESS_CODE

    def is_for_access(self):
        """Returns ``True`` if the file with UUID ``self.file_uuid`` is "for"
        access.
        """
        if (self.is_manually_normalized_access_derivative or
                self.file_model.filegrpuse == 'access'):
            return True
        return False

    purpose = 'checkingPolicy'

    def get_manually_normalized_access_derivative_file_uuid(self):
        """If the file-to-be-policy-checked is a manually normalized access
        derivative it will have no file UUID in the database. We therefore have
        to retrieve the UUID of the original file that was format-identified,
        i.e., the file that was in manualNormalization/access/, which we do by
        querying the database based on the original location of the file. This
        file UUID is needed so that we can get the format (PRONOM id) in order
        to retrieve the appropriate policy-check FPR rule for this type of file
        (see ``self._get_rules()``).
        """
        manually_normalized_file_name = os.path.basename(self.file_path)[37:]
        manually_normalized_file_path = \
            '%transferDirectory%objects/manualNormalization/access/{}'.format(
                manually_normalized_file_name)
        try:
            return File.objects.get(
                originallocation=manually_normalized_file_path,
                sip_id=self.sip_uuid).uuid
        except (File.DoesNotExist, File.MultipleObjectsReturned):
            return None

    def _get_rules(self):
        """Return the FPR rules with purpose ``self.purpose`` and that apply to
        the type/format of file given as input.
        """
        file_uuid = self.file_uuid
        if self.is_manually_normalized_access_derivative:
            file_uuid = \
                self.get_manually_normalized_access_derivative_file_uuid()
        try:
            fmt = FormatVersion.active.get(
                fileformatversion__file_uuid=file_uuid)
        except FormatVersion.DoesNotExist:
            rules = fmt = None
        if fmt:
            rules = FPRule.active.filter(format=fmt.uuid, purpose=self.purpose)
        # Check for default rules.
        if not rules:
            rules = FPRule.active.filter(
                purpose='default_{}'.format(self.purpose))
        return rules

    def save_to_logs_dir(self, output):
        """Save the MediaConch policy file as well as the raw MediaConch stdout
        for the target file to the logs/ directory of the SIP.
        """
        self.save_stdout_to_logs_dir(output)
        self.save_policy_to_logs_dir(output)

    def save_stdout_to_logs_dir(self, output):
        """Save the output of running MediaConch's policy checker against the
        input file to a subdirectory of the logs/policies directory of the SIP.
        """
        policy_filename = output.get('policy')
        mc_stdout = output.get('stdout')
        if policy_filename and mc_stdout and self.sip_policies_dir:
            purpose_dir = {
                'checkingPresDerivativePolicy': 'preservationDerivatives'
            }.get(self.purpose, 'originals')
            purpose_path = os.path.join(self.sip_policies_dir, purpose_dir)
            if not os.path.isdir(purpose_path):
                os.makedirs(purpose_path)
            policy_dirname, _ = os.path.splitext(policy_filename)
            policy_path = os.path.join(purpose_path, policy_dirname)
            if not os.path.isdir(policy_path):
                os.makedirs(policy_path)
            filename = os.path.basename(self.file_path)
            stdout_path = os.path.join(policy_path, '{}.xml'.format(filename))
            with open(stdout_path, 'w') as f:
                f.write(mc_stdout)

    def save_policy_to_logs_dir(self, output):
        """Save the policy file ``policy_filename`` to the logs/policies/
        directory of the SIP, if it is not there already.
        """
        policy_filename = output.get('policy')
        if policy_filename and self.sip_policies_dir:
            dst = os.path.join(self.sip_policies_dir, policy_filename)
            if not os.path.isfile(dst):
                src = os.path.join(self.policies_dir, policy_filename)
                if not os.path.isfile(src):
                    print('Warning: unable to find policy file at'
                          ' {}'.format(src))
                else:
                    shutil.copyfile(src, dst)

    @property
    def sip_logs_dir(self):
        """Return the absolute path the logs/ directory of the SIP that the
        target file is a part of.
        """
        if self._sip_logs_dir:
            return self._sip_logs_dir
        try:
            sip_model = SIP.objects.get(uuid=self.sip_uuid)
        except (SIP.DoesNotExist, SIP.MultipleObjectsReturned):
            print('Warning: unable to retrieve SIP model corresponding to SIP'
                  ' UUID {}'.format(self.sip_uuid), file=sys.stderr)
            return None
        else:
            sip_path = sip_model.currentpath.replace(
                '%sharedPath%', self.shared_path, 1)
            logs_dir = os.path.join(sip_path, 'logs')
            if os.path.isdir(logs_dir):
                self._sip_logs_dir = logs_dir
                return logs_dir
            print('Warning: unable to find a logs/ directory in the SIP'
                  ' with UUID {}'.format(self.sip_uuid), file=sys.stderr)
            return None

    @property
    def sip_policies_dir(self):
        if self._sip_policies_dir:
            return self._sip_policies_dir
        if self.sip_logs_dir:
            _sip_policies_dir = os.path.join(self.sip_logs_dir, 'policies')
            if os.path.isdir(_sip_policies_dir):
                self._sip_policies_dir = _sip_policies_dir
            else:
                try:
                    os.makedirs(_sip_policies_dir)
                except:
                    pass
                else:
                    self._sip_policies_dir = _sip_policies_dir
        return self._sip_policies_dir

    def _execute_rule_command(self, rule):
        """Execute the FPR command of FPR rule ``rule`` against the file passed
        in to this client script. The output of that command determines what we
        print to stdout and stderr, and the nature of the validation event that
        we save to the db. We also copy the MediaConch policy file to the logs/
        directory of the AIP if it has not already been copied there.
        """
        result = 'passed'
        command_to_execute, args = self._get_command_to_execute(rule)
        print('Running', rule.command.description)
        exitstatus, stdout, stderr = executeOrRun(
            rule.command.script_type, command_to_execute, arguments=args,
            printing=False)
        output = json.loads(stdout)
        # TODO: will add originals-checking to this tuple in future
        if self.purpose in ('checkingPresDerivativePolicy',):
            self.save_to_logs_dir(output)
        if exitstatus == 0:
            print('Command {} completed with output {}'.format(
                  rule.command.description, stdout))
        else:
            print('Command {} failed with exit status {}; stderr:'.format(
                  rule.command.description, exitstatus), stderr,
                  file=sys.stderr)
            return 'failed'
        event_detail = ('program="{tool.description}";'
                        ' version="{tool.version}"'.format(
                            tool=rule.command.tool))
        if ('Check against policy' in rule.command.description and
                'MediaConch' in rule.command.description and
                output.get('eventOutcomeInformation') != 'pass'):
            print('Command {descr} returned a non-pass outcome for the policy'
                  ' check;\n\noutcome: {outcome}\n\ndetails: {details}.'
                  .format(
                      descr=rule.command.description,
                      outcome=output.get('eventOutcomeInformation'),
                      details=output.get('eventOutcomeDetailNote')),
                  file=sys.stderr)
            result = 'failed'
        print('Creating policy checking event for {} ({})'
              .format(self.file_path, self.file_uuid))
        # Manually-normalized access derivatives have no file UUID so we can't
        # create a validation event for them. TODO/QUESTION: should we use the
        # UUID that was assigned to the manually normalized derivative during
        # transfer, i.e., the one that we retrieve in
        # ``get_manually_normalized_access_derivative_file_uuid`` above?
        if not self.is_manually_normalized_access_derivative:
            databaseFunctions.insertIntoEvents(
                fileUUID=self.file_uuid,
                eventType='validation',  # From PREMIS controlled vocab.
                eventDetail=event_detail,
                eventOutcome=output.get('eventOutcomeInformation'),
                eventOutcomeDetailNote=output.get('eventOutcomeDetailNote'),
            )
        return result

    def _get_command_to_execute(self, rule):
        """Return a 2-tuple consisting of a) the FPR rule ``rule``'s command
        and b) a list of arguments to pass to it.
        """
        if rule.command.script_type in ('bashScript', 'command'):
            return (replace_string_values(rule.command.command,
                                          file_=self.file_uuid,
                                          sip=self.sip_uuid, type_='file'),
                    [])
        else:
            return (rule.command.command, [self.file_path, self.policies_dir])

if __name__ == '__main__':
    logger = get_script_logger(
        "archivematica.mcp.client.policyCheck")
    file_path = sys.argv[1]
    file_uuid = sys.argv[2]
    sip_uuid = sys.argv[3]
    shared_path = sys.argv[4]
    policy_checker = PolicyChecker(file_path, file_uuid, sip_uuid,
                                   shared_path)
    sys.exit(policy_checker.check())