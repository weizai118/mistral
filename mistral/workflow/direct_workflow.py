# Copyright 2015 - Mirantis, Inc.
#
#    Licensed under the Apache License, Version 2.0 (the "License");
#    you may not use this file except in compliance with the License.
#    You may obtain a copy of the License at
#
#        http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS,
#    WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#    See the License for the specific language governing permissions and
#    limitations under the License.

from oslo_log import log as logging

from mistral import exceptions as exc
from mistral import expressions as expr
from mistral import utils
from mistral.workflow import base
from mistral.workflow import commands
from mistral.workflow import data_flow
from mistral.workflow import states
from mistral.workflow import utils as wf_utils


LOG = logging.getLogger(__name__)


class DirectWorkflowController(base.WorkflowController):
    """'Direct workflow' handler.

    This handler implements the workflow pattern which is based on
    direct transitions between tasks, i.e. after each task completion
    a decision should be made which tasks should run next based on
    result of task execution.
    Note, that tasks can run in parallel. For example, if there's a workflow
    consisting of three tasks 'A', 'B' and 'C' where 'A' starts first then
    'B' and 'C' can start second if certain associated with transition
    'A'->'B' and 'A'->'C' evaluate to true.
    """

    __workflow_type__ = "direct"

    def _get_upstream_task_executions(self, task_spec):
        return list(
            filter(
                lambda t_e: self._is_upstream_task_execution(task_spec, t_e),
                wf_utils.find_task_executions_by_specs(
                    self.wf_ex,
                    self.wf_spec.find_inbound_task_specs(task_spec)
                )
            )
        )

    def _is_upstream_task_execution(self, t_spec, t_ex_candidate):
        if not states.is_completed(t_ex_candidate.state):
            return False

        if not t_spec.get_join():
            return not t_ex_candidate.processed

        induced_state = self._get_induced_join_state(
            self.wf_spec.get_tasks()[t_ex_candidate.name],
            t_spec
        )

        return induced_state == states.RUNNING

    def _find_next_commands(self, task_ex=None):
        cmds = super(DirectWorkflowController, self)._find_next_commands(
            task_ex
        )

        if not self.wf_ex.task_executions:
            return self._find_start_commands()

        if task_ex:
            task_execs = [task_ex]
        else:
            task_execs = [
                t_ex for t_ex in self.wf_ex.task_executions
                if states.is_completed(t_ex.state) and not t_ex.processed
            ]

        for t_ex in task_execs:
            cmds.extend(self._find_next_commands_for_task(t_ex))

        return cmds

    def _find_start_commands(self):
        return [
            commands.RunTask(
                self.wf_ex,
                self.wf_spec,
                t_s,
                self.get_task_inbound_context(t_s)
            )
            for t_s in self.wf_spec.find_start_tasks()
        ]

    def _find_next_commands_for_task(self, task_ex):
        """Finds next commands based on the state of the given task.

        :param task_ex: Task execution for which next commands need
            to be found.
        :return: List of workflow commands.
        """

        cmds = []

        for t_n, params in self._find_next_tasks(task_ex):
            t_s = self.wf_spec.get_tasks()[t_n]

            if not (t_s or t_n in commands.RESERVED_CMDS):
                raise exc.WorkflowException("Task '%s' not found." % t_n)
            elif not t_s:
                t_s = self.wf_spec.get_tasks()[task_ex.name]

            cmd = commands.create_command(
                t_n,
                self.wf_ex,
                self.wf_spec,
                t_s,
                data_flow.evaluate_task_outbound_context(task_ex),
                params
            )

            self._configure_if_join(cmd)

            cmds.append(cmd)

        LOG.debug("Found commands: %s" % cmds)

        return cmds

    def _configure_if_join(self, cmd):
        if not isinstance(cmd, commands.RunTask):
            return

        if not cmd.task_spec.get_join():
            return

        cmd.unique_key = self._get_join_unique_key(cmd)
        cmd.wait = True

    def _get_join_unique_key(self, cmd):
        return 'join-task-%s-%s' % (self.wf_ex.id, cmd.task_spec.get_name())

    # TODO(rakhmerov): Need to refactor this method to be able to pass tasks
    # whose contexts need to be merged.
    def evaluate_workflow_final_context(self):
        ctx = {}

        for t_ex in self._find_end_tasks():
            ctx = utils.merge_dicts(
                ctx,
                data_flow.evaluate_task_outbound_context(t_ex)
            )

        return ctx

    def get_logical_task_state(self, task_ex):
        task_spec = self.wf_spec.get_tasks()[task_ex.name]

        if not task_spec.get_join():
            # A simple 'non-join' task does not have any preconditions
            # based on state of other tasks so its logical state always
            # equals to its real state.
            return task_ex.state, task_ex.state_info

        return self._get_join_logical_state(task_spec)

    def is_error_handled_for(self, task_ex):
        return bool(self.wf_spec.get_on_error_clause(task_ex.name))

    def all_errors_handled(self):
        for t_ex in wf_utils.find_error_task_executions(self.wf_ex):

            tasks_on_error = self._find_next_tasks_for_clause(
                self.wf_spec.get_on_error_clause(t_ex.name),
                data_flow.evaluate_task_outbound_context(t_ex)
            )

            if not tasks_on_error:
                return False

        return True

    def _find_end_tasks(self):
        return list(
            filter(
                lambda t_ex: not self._has_outbound_tasks(t_ex),
                wf_utils.find_successful_task_executions(self.wf_ex)
            )
        )

    def _has_outbound_tasks(self, task_ex):
        # In order to determine if there are outbound tasks we just need
        # to calculate next task names (based on task outbound context)
        # and remove all engine commands. To do the latter it's enough to
        # check if there's a corresponding task specification for a task name.
        return bool([
            t_name for t_name in self._find_next_task_names(task_ex)
            if self.wf_spec.get_tasks()[t_name]
        ])

    def _find_next_task_names(self, task_ex):
        return [t[0] for t in self._find_next_tasks(task_ex)]

    def _find_next_tasks(self, task_ex):
        t_state = task_ex.state
        t_name = task_ex.name

        ctx = data_flow.evaluate_task_outbound_context(task_ex)

        t_names_and_params = []

        if states.is_completed(t_state):
            t_names_and_params += (
                self._find_next_tasks_for_clause(
                    self.wf_spec.get_on_complete_clause(t_name),
                    ctx
                )
            )

        if t_state == states.ERROR:
            t_names_and_params += (
                self._find_next_tasks_for_clause(
                    self.wf_spec.get_on_error_clause(t_name),
                    ctx
                )
            )

        elif t_state == states.SUCCESS:
            t_names_and_params += (
                self._find_next_tasks_for_clause(
                    self.wf_spec.get_on_success_clause(t_name),
                    ctx
                )
            )

        return t_names_and_params

    @staticmethod
    def _find_next_tasks_for_clause(clause, ctx):
        """Finds next tasks names.

         This method finds next task(command) base on given {name: condition}
         dictionary.

        :param clause: Dictionary {task_name: condition} taken from
            'on-complete', 'on-success' or 'on-error' clause.
        :param ctx: Context that clause expressions should be evaluated
            against of.
        :return: List of task(command) names.
        """
        if not clause:
            return []

        return [
            (t_name, expr.evaluate_recursively(params, ctx))
            for t_name, condition, params in clause
            if not condition or expr.evaluate(condition, ctx)
        ]

    def _get_join_logical_state(self, task_spec):
        # TODO(rakhmerov): We need to use task_ex instead of task_spec
        # in order to cover a use case when there's more than one instance
        # of the same 'join' task in a workflow.
        join_expr = task_spec.get_join()

        in_task_specs = self.wf_spec.find_inbound_task_specs(task_spec)

        if not in_task_specs:
            return states.RUNNING

        # List of tuples (task_name, state).
        induced_states = [
            (t_s.get_name(), self._get_induced_join_state(t_s, task_spec))
            for t_s in in_task_specs
        ]

        def count(state):
            return len(list(filter(lambda s: s[1] == state, induced_states)))

        error_count = count(states.ERROR)
        running_count = count(states.RUNNING)
        total_count = len(induced_states)

        def _blocked_message():
            return (
                'Blocked by tasks: %s' %
                [s[0] for s in induced_states if s[1] == states.WAITING]
            )

        def _failed_message():
            return (
                'Failed by tasks: %s' %
                [s[0] for s in induced_states if s[1] == states.ERROR]
            )

        # If "join" is configured as a number or 'one'.
        if isinstance(join_expr, int) or join_expr == 'one':
            cardinality = 1 if join_expr == 'one' else join_expr

            if running_count >= cardinality:
                return states.RUNNING, None

            # E.g. 'join: 3' with inbound [ERROR, ERROR, RUNNING, WAITING]
            # No chance to get 3 RUNNING states.
            if error_count > (total_count - cardinality):
                return states.ERROR, _failed_message()

            return states.WAITING, _blocked_message()

        if join_expr == 'all':
            if total_count == running_count:
                return states.RUNNING, None

            if error_count > 0:
                return states.ERROR, _failed_message()

            return states.WAITING, _blocked_message()

        raise RuntimeError('Unexpected join expression: %s' % join_expr)

    # TODO(rakhmerov): Method signature is incorrect given that
    # we may have multiple task executions for a task. It should
    # accept inbound task execution rather than a spec.
    def _get_induced_join_state(self, inbound_task_spec, join_task_spec):
        join_task_name = join_task_spec.get_name()

        in_task_ex = self._find_task_execution_by_spec(inbound_task_spec)

        if not in_task_ex:
            if self._possible_route(inbound_task_spec):
                return states.WAITING
            else:
                return states.ERROR

        if not states.is_completed(in_task_ex.state):
            return states.WAITING

        if join_task_name not in self._find_next_task_names(in_task_ex):
            return states.ERROR

        return states.RUNNING

    def _find_task_execution_by_spec(self, task_spec):
        in_t_execs = wf_utils.find_task_executions_by_spec(
            self.wf_ex,
            task_spec
        )

        # TODO(rakhmerov): Temporary hack. See the previous comment.
        return in_t_execs[-1] if in_t_execs else None

    def _possible_route(self, task_spec):
        # TODO(rakhmerov): In some cases this method will be expensive because
        # it uses a multistep recursive search with DB queries.
        # It will be optimized with Workflow Execution Graph moving forward.
        in_task_specs = self.wf_spec.find_inbound_task_specs(task_spec)

        if not in_task_specs:
            return True

        for t_s in in_task_specs:
            t_ex = self._find_task_execution_by_spec(t_s)

            if not t_ex:
                if self._possible_route(t_s):
                    return True
            else:
                t_name = task_spec.get_name()

                if (not states.is_completed(t_ex.state) or
                        t_name in self._find_next_task_names(t_ex)):
                    return True

        return False
