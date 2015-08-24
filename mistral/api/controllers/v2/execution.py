# Copyright 2013 - Mirantis, Inc.
# Copyright 2015 - StackStorm, Inc.
# Copyright 2015 Huawei Technologies Co., Ltd.
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

import json
from oslo_log import log as logging
import pecan
from pecan import rest
from wsme import types as wtypes
import wsmeext.pecan as wsme_pecan

from mistral.api.controllers import resource
from mistral.api.controllers.v2 import task
from mistral.api.controllers.v2 import types
from mistral.db.v2 import api as db_api
from mistral.engine import rpc
from mistral import exceptions as exc
from mistral.utils import rest_utils
from mistral.workflow import states


LOG = logging.getLogger(__name__)

# TODO(rakhmerov): Make sure to make all needed renaming on public API.


class Execution(resource.Resource):
    """Execution resource."""

    id = wtypes.text
    "id is immutable and auto assigned."

    workflow_name = wtypes.text
    "reference to workflow definition"

    description = wtypes.text
    "description of workflow execution."

    params = wtypes.text
    "params define workflow type specific parameters. For example, reverse \
    workflow takes one parameter 'task_name' that defines a target task."

    state = wtypes.text
    "state can be one of: RUNNING, SUCCESS, ERROR, PAUSED"

    state_info = wtypes.text
    "an optional state information string"

    input = wtypes.text
    "input is a JSON structure containing workflow input values."
    output = wtypes.text
    "output is a workflow output."

    created_at = wtypes.text
    updated_at = wtypes.text

    # Context is a JSON object but since WSME doesn't support arbitrary
    # dictionaries we have to use text type convert to json and back manually.
    def to_dict(self):
        d = super(Execution, self).to_dict()

        if d.get('input'):
            d['input'] = json.loads(d['input'])

        if d.get('output'):
            d['output'] = json.loads(d['output'])

        if d.get('params'):
            d['params'] = json.loads(d['params'])

        return d

    @classmethod
    def from_dict(cls, d):
        e = cls()

        for key, val in d.items():
            if hasattr(e, key):
                # Nonetype check for dictionary must be explicit
                if key in ['input', 'output', 'params'] and val is not None:
                    val = json.dumps(val)
                setattr(e, key, val)

        return e

    @classmethod
    def sample(cls):
        return cls(id='123e4567-e89b-12d3-a456-426655440000',
                   workflow_name='flow',
                   description='this is the first execution.',
                   state='SUCCESS',
                   input='{}',
                   output='{}',
                   params='{"env": {"k1": "abc", "k2": 123}}',
                   created_at='1970-01-01T00:00:00.000000',
                   updated_at='1970-01-01T00:00:00.000000')


class Executions(resource.ResourceList):
    """A collection of Execution resources."""

    executions = [Execution]

    def __init__(self, **kwargs):
        self._type = 'executions'

        super(Executions, self).__init__(**kwargs)

    @classmethod
    def sample(cls):
        executions_sample = cls()
        executions_sample.executions = [Execution.sample()]
        executions_sample.next = "http://localhost:8989/v2/executions?" \
                                 "sort_keys=id,workflow_name&" \
                                 "sort_dirs=asc,desc&limit=10&" \
                                 "marker=123e4567-e89b-12d3-a456-426655440000"

        return executions_sample


class ExecutionsController(rest.RestController):
    tasks = task.ExecutionTasksController()

    @rest_utils.wrap_wsme_controller_exception
    @wsme_pecan.wsexpose(Execution, wtypes.text)
    def get(self, id):
        """Return the specified Execution."""
        LOG.info("Fetch execution [id=%s]" % id)

        return Execution.from_dict(db_api.get_workflow_execution(id).to_dict())

    @rest_utils.wrap_wsme_controller_exception
    @wsme_pecan.wsexpose(Execution, wtypes.text, body=Execution)
    def put(self, id, execution):
        """Update the specified Execution.

        :param id: execution ID.
        :param execution: Execution objects
        """
        LOG.info("Update execution [id=%s, execution=%s]" %
                 (id, execution))
        db_api.ensure_workflow_execution_exists(id)

        new_state = execution.state
        new_description = execution.description
        msg = execution.state_info

        # Currently we can change only state or description.
        if (not (new_state or new_description) or
                (new_state and new_description)):
            raise exc.DataAccessException(
                "Only state or description of execution can be changed. "
                "But they can not be changed at the same time."
            )

        if new_description:
            wf_ex = db_api.update_workflow_execution(
                id,
                {"description": new_description}
            )

        elif new_state == states.PAUSED:
            wf_ex = rpc.get_engine_client().pause_workflow(id)
        elif new_state == states.RUNNING:
            wf_ex = rpc.get_engine_client().resume_workflow(id)
        elif new_state in [states.SUCCESS, states.ERROR]:
            wf_ex = rpc.get_engine_client().stop_workflow(id, new_state, msg)
        else:
            # To prevent changing state in other cases throw a message.
            raise exc.DataAccessException(
                "Can not change state to %s. Allowed states are: '%s" %
                (new_state, ", ".join([states.RUNNING, states.PAUSED,
                 states.SUCCESS, states.ERROR]))
            )

        return Execution.from_dict(
            wf_ex if isinstance(wf_ex, dict) else wf_ex.to_dict()
        )

    @rest_utils.wrap_wsme_controller_exception
    @wsme_pecan.wsexpose(Execution, body=Execution, status_code=201)
    def post(self, execution):
        """Create a new Execution.

        :param execution: Execution object with input content.
        """
        LOG.info("Create execution [execution=%s]" % execution)

        engine = rpc.get_engine_client()
        exec_dict = execution.to_dict()

        result = engine.start_workflow(
            exec_dict['workflow_name'],
            exec_dict.get('input'),
            exec_dict.get('description', ''),
            **exec_dict.get('params') or {}
        )

        return Execution.from_dict(result)

    @rest_utils.wrap_wsme_controller_exception
    @wsme_pecan.wsexpose(None, wtypes.text, status_code=204)
    def delete(self, id):
        """Delete the specified Execution."""
        LOG.info("Delete execution [id=%s]" % id)

        return db_api.delete_workflow_execution(id)

    @wsme_pecan.wsexpose(Executions, types.uuid, int, types.uniquelist,
                         types.list)
    def get_all(self, marker=None, limit=None, sort_keys='created_at',
                sort_dirs='desc'):
        """Return all Executions.

        :param marker: Optional. Pagination marker for large data sets.
        :param limit: Optional. Maximum number of resources to return in a
                      single result. Default value is None for backward
                      compatability.
        :param sort_keys: Optional. Columns to sort results by.
                          Default: created_at, which is backward compatible.
        :param sort_dirs: Optional. Directions to sort corresponding to
                          sort_keys, "asc" or "desc" can be choosed.
                          Default: desc. The length of sort_dirs can be equal
                          or less than that of sort_keys.
        """
        LOG.info("Fetch executions. marker=%s, limit=%s, sort_keys=%s, "
                 "sort_dirs=%s", marker, limit, sort_keys, sort_dirs)

        rest_utils.validate_query_params(limit, sort_keys, sort_dirs)

        marker_obj = None

        if marker:
            marker_obj = db_api.get_workflow_execution(marker)

        db_workflow_exs = db_api.get_workflow_executions(
            limit=limit,
            marker=marker_obj,
            sort_keys=sort_keys,
            sort_dirs=sort_dirs
        )

        wf_executions = [
            Execution.from_dict(db_model.to_dict())
            for db_model in db_workflow_exs
        ]

        return Executions.convert_with_links(
            wf_executions,
            limit,
            pecan.request.host_url,
            sort_keys=','.join(sort_keys),
            sort_dirs=','.join(sort_dirs)
        )
