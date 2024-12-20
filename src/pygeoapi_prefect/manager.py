"""pygeoapi process manager based on Prefect."""

import json
import logging
import uuid
from typing import (
    Any,
    Optional,
    Dict, Tuple
)

import anyio
import httpx
from flask import g
from prefect import flow
from prefect.client.orchestration import get_client
from prefect.client.schemas import FlowRun
from prefect.deployments import run_deployment
from prefect.exceptions import MissingResult, UnfinishedRun
from prefect.server.schemas import filters
from prefect.server.schemas.core import Flow
from prefect.server.schemas.states import StateType
from prefect.task_runners import ConcurrentTaskRunner

from pygeoapi.process.base import (
    BaseProcessor,
    ProcessorExecuteError,
    JobNotFoundError
)
from pygeoapi.process.manager.base import BaseManager
from pygeoapi.util import JobStatus, RequestedResponse, Subscriber

from .process.base import BasePrefectProcessor
from .schemas import (
    ExecuteRequest,
    JobStatusInfoInternal,
    ProcessExecutionMode,
    OutputExecutionResultInternal,
    RequestedProcessExecutionMode,
)

logger = logging.getLogger(__name__)


class PrefectManager(BaseManager):
    """Prefect-powered pygeoapi manager.

    This manager equates pygeoapi jobs with prefect flow runs.

    Although flow runs have a `flow_run_id`, which could be used as the
    pygeoapi `job_id`, this manager does not use them and instead relies on
    setting a flow run's `name` and use that as the equivalent to the pygeoapi
    job id.
    """

    # is_async: bool = True
    _flow_run_name_prefix = "pygeoapi_job_"

    def __init__(self, manager_def: dict):
        super().__init__(manager_def)
        self.is_async = True

    prefect_state_map = {
        StateType.SCHEDULED: JobStatus.accepted,
        StateType.PENDING: JobStatus.accepted,
        StateType.RUNNING: JobStatus.running,
        StateType.COMPLETED: JobStatus.successful,
        StateType.FAILED: JobStatus.failed,
        StateType.CANCELLED: JobStatus.dismissed,
        StateType.CRASHED: JobStatus.failed,
        StateType.PAUSED: JobStatus.accepted,
        StateType.CANCELLING: JobStatus.dismissed,
    }

    def add_job(self, job_metadata: dict) -> str:
        """Add a job.

        This method is part of the ``pygeoapi.BaseManager`` API. However, in
        the context of prefect we do not need it.
        """
        raise NotImplementedError

    def update_job(self, job_id: str, update_dict: dict) -> bool:
        """Update an existing job.

        This method is part of the ``pygeoapi.BaseManager`` API. However, in
        the context of prefect we do not need it.
        """
        raise NotImplementedError

    def get_jobs(
            self,
            type_: list[str] | None = None,
            process_id: list[str] | None = None,
            status: list[JobStatus] | None = None,
            date_time: str | None = None,
            min_duration_seconds: int | None = None,
            max_duration_seconds: int | None = None,
            limit: int | None = 10,
            offset: int | None = 0,
    ) -> list[JobStatusInfoInternal]:
        """Get a list of jobs, optionally filtered by relevant parameters.

        Job list filters are not implemented in pygeoapi yet though, so for
        the moment it is not possible to use them for filtering jobs.
        """
        if status is not None:
            prefect_states = []
            for k, v in self.prefect_state_map.items():
                if status == v:
                    prefect_states.append(k)
        else:
            prefect_states = [
                StateType.RUNNING,
                StateType.COMPLETED,
                StateType.CRASHED,
                StateType.CANCELLED,
                StateType.CANCELLING,
            ]
        try:
            flow_runs = anyio.run(
                _get_prefect_flow_runs, prefect_states, self._flow_run_name_prefix
            )
        except httpx.ConnectError as err:
            # TODO: would be more explicit to raise an exception,
            #  but pygeoapi is not able to handle this yet
            logger.error(f"Could not connect to prefect server: {str(err)}")
            flow_runs = []

        seen_flows = {}
        jobs = []
        for flow_run in flow_runs:
            if flow_run.flow_id not in seen_flows:
                flow = anyio.run(_get_prefect_flow, flow_run.flow_id)
                seen_flows[flow_run.flow_id] = flow
            job_status = self._flow_run_to_job_status(
                flow_run, seen_flows[flow_run.flow_id]
            )
            jobs.append(self._job_status_to_external(job_status))
        return {
            'jobs': jobs,
            'numberMatched': len(jobs)
        }

    def _job_id_to_flow_run_name(self, job_id: str) -> str:
        """Convert input job_id onto corresponding prefect flow_run name."""
        return f"{self._flow_run_name_prefix}{job_id}"

    def _flow_run_name_to_job_id(self, flow_run_name: str) -> str:
        """Convert input flow_run name onto corresponding pygeoapi job_id."""
        return flow_run_name.replace(self._flow_run_name_prefix, "")

    def _job_status_to_external(self, internal: JobStatusInfoInternal) -> Dict:
        """Convert from JobStatusInfoInternal to pygeoapi dict format"""

        generated_outputs: Dict
        if internal.generated_outputs is None:
            generated_outputs = {}
        else:
            generated_outputs = {
                elem[0]: elem[1].model_dump() for elem in internal.generated_outputs.items()
            }

        return {
            'process_id': internal.process_id,
            'identifier': internal.job_id,
            'status': internal.status.value,
            'message': internal.message,
            'progress': internal.progress,
            'parameters': {
                "negotiated_execution_mode": internal.negotiated_execution_mode.value
                if internal.negotiated_execution_mode is not None else "undefined",
                "generated_outputs": generated_outputs,
                "requested_response_type": internal.requested_response_type.value
                if internal.requested_response_type is not None else "undefined",
            },
            "mimetype": None,
            'job_start_datetime': internal.started,
            'job_end_datetime': internal.finished
        }

    def get_job_internal(self, job_id: str) -> JobStatusInfoInternal:
        """Get job details."""
        flow_run_name = self._job_id_to_flow_run_name(job_id)
        try:
            flow_run_details = anyio.run(_get_prefect_flow_run, flow_run_name)
        except httpx.ConnectError as err:
            # TODO: would be more explicit to raise an exception,
            #  but pygeoapi is not able to handle this yet
            logger.error(f"Could not connect to prefect server: {str(err)}")
            flow_run_details = None

        if flow_run_details is None:
            raise JobNotFoundError()
        else:
            flow_run, prefect_flow = flow_run_details
            return self._flow_run_to_job_status(flow_run, prefect_flow)

    def get_job(self, job_id: str) -> Dict:
        return self._job_status_to_external(self.get_job_internal(job_id))

    def delete_job(  # type: ignore [empty-body]
            self, job_id: str
    ) -> JobStatusInfoInternal:
        """Delete a job and associated results/ouptuts."""
        pass

    def _select_execution_mode(
            self,
            requested: Optional[RequestedProcessExecutionMode],
            processor: BaseProcessor
    ) -> tuple[ProcessExecutionMode, dict[str, str]]:
        """Select the execution mode to be employed

        The execution mode to use depends on a number of factors:

        - what mode, if any, was requested by the client?
        - does the process support sync and async execution modes?
        - does the process manager support sync and async modes?
        """
        if requested is not None:
            if requested.value == RequestedProcessExecutionMode.respond_async.value:
                # client wants async - do we support it?
                process_supports_async = (
                        ProcessExecutionMode.async_execute.value in
                        processor.process_description.job_control_options
                )
                if self.is_async and process_supports_async:
                    chosen_mode = ProcessExecutionMode.async_execute
                    additional_headers = {
                        'Preference-Applied': (
                            RequestedProcessExecutionMode.respond_async.value)
                    }
                else:
                    chosen_mode = ProcessExecutionMode.sync_execute
                    additional_headers = {
                        'Preference-Applied': (
                            RequestedProcessExecutionMode.wait.value)
                    }
            else:
                # client wants sync - pygeoapi implicitly supports sync mode
                logger.debug('Synchronous execution')
                chosen_mode = ProcessExecutionMode.sync_execute
                additional_headers = {
                    'Preference-Applied': RequestedProcessExecutionMode.wait.value}
        else:  # client has no preference
            # according to OAPI - Processes spec we ought to respond with sync
            logger.debug('Synchronous execution')
            chosen_mode = ProcessExecutionMode.sync_execute
            additional_headers = {}

        has_deployment = getattr(processor, "deployment_info", None) is not None
        if chosen_mode == ProcessExecutionMode.async_execute and not has_deployment:
            logger.warning(
                "Cannot run asynchronously on non-deployed processes - "
                "Switching to sync"
            )
            chosen_mode = ProcessExecutionMode.sync_execute
            additional_headers[
                "Preference-Applied"
            ] = RequestedProcessExecutionMode.wait.value
        return chosen_mode, additional_headers

    def _execute_prefect_processor(
            self,
            job_id: str,
            processor: BasePrefectProcessor,
            chosen_mode: ProcessExecutionMode,
            execution_request: ExecuteRequest,
    ) -> JobStatusInfoInternal:
        """Execute custom prefect processor.

        Execution is triggered by one of three ways:

        - if there is a deployment for the process, then run wherever the
          deployment is housed. Depending on the chosen execution mode, runs
          either:
            - asynchronously
            - synchronously
        - If there is no deployment for the process, then run locally and
          synchronously
        """
        run_params = {
            "job_id": job_id,
            "result_storage_block": processor.result_storage_block,
            "result_storage_basepath": processor.result_storage_basepath,
            "process_description": processor.process_description.model_dump(
                by_alias=True, exclude_none=True
            ),
            "execution_request": execution_request.model_dump(
                by_alias=True, exclude_none=True
            ),
        }
        if processor.deployment_info is None:  # will run locally and sync
            flow_fn = processor.process_flow
            flow_fn.flow_run_name = self._job_id_to_flow_run_name(job_id)
            flow_fn.persist_result = True
            flow_fn.log_prints = True
            if chosen_mode == ProcessExecutionMode.sync_execute:
                logger.info("synchronous execution without deployment")
                try:
                    flow_fn(**run_params)
                except Exception as e:
                    print(e)
            else:
                raise NotImplementedError("Cannot run regular processes async")
        else:
            # if there is a deployment, then we must rely on the flow function
            # having been explicitly configured to:
            # - persist results
            # - log prints
            #
            # deployed flows cannot be modified in the same way as local ones
            deployment_name = (
                f"{processor.process_description.id}/{processor.deployment_info.name}"
            )
            run_kwargs = {
                "name": deployment_name,
                "parameters": run_params,
                "flow_run_name": self._job_id_to_flow_run_name(job_id),
            }
            if chosen_mode == ProcessExecutionMode.sync_execute:
                logger.info("synchronous execution with deployment")
                run_deployment(**run_kwargs)
            else:
                logger.info("asynchronous execution")
                run_deployment(
                    **run_kwargs, timeout=0  # has the effect of returning immediately
                )
        updated_status_info = self.get_job_internal(job_id)
        logger.info(f"updated_status_info: {updated_status_info}")
        return updated_status_info

    def _execute_base_processor(
            self,
            job_id: str,
            processor: BaseProcessor,
            execution_request: ExecuteRequest,
            # ) -> JobStatusInfoInternal:
    ) -> tuple[str, Any, JobStatus]:
        """Execute a regular pygeoapi process via prefect.

        This wraps the pygeoapi processor.execute() call in a prefect flow,
        which is then run locally.

        After the process is executed, this method mimics the default pygeoapi
        manager's behavior of saving generated outputs to disk.
        """

        execution_parameters = execution_request.model_dump(
            by_alias=True, exclude_none=True)
        input_parameters = execution_parameters.get("inputs", {})
        logger.warning(f"{execution_parameters=}")
        logger.warning(f"{input_parameters=}")

        @flow(
            name=processor.metadata["id"],
            version=processor.metadata["version"],
            flow_run_name=self._job_id_to_flow_run_name(job_id),
            persist_result=True,
            log_prints=True,
            validate_parameters=True,
            task_runner=ConcurrentTaskRunner(),  # this should be configurable
            retries=0,  # this should be configurable
            retry_delay_seconds=0,  # this should be configurable
            timeout_seconds=None,  # this should be configurable
        )
        def executor(data_: dict):
            """Run a vanilla pygeoapi process as a prefect flow.

            Since we are adapting a vanilla pygeoapi processor to run with
            prefect, we must ensure the processor is called with the expected
            parameters.
            """
            return processor.execute(data_)

        try:
            output_media_type, generated_output = executor(input_parameters)
        except RuntimeError as err:
            # TODO: Change the exception once pygeoapi gets
            #  process-execution-related exceptions in its main process.exceptions
            #  module
            raise ProcessorExecuteError(str(err)) from err
            # raise exceptions.ProcessError() from err
        else:
            # now try to save outputs to local disk, similarly to what the
            # `pygeoapi.BaseManager._execute_handler_sync()` method does
            filename = f"{processor.metadata['id']}-{job_id}"
            job_path = self.output_dir / filename if self.output_dir is not None else None

            if job_path is not None:
                logger.debug(f'writing output to {job_path}')
                if isinstance(generated_output, dict):
                    mode = 'w'
                    data = json.dumps(generated_output, sort_keys=True, indent=4)
                    encoding = 'utf-8'
                else:
                    mode = 'wb'
                    data = generated_output
                    encoding = None
                with job_path.open(mode=mode, encoding=encoding) as fh:
                    fh.write(data)
            return output_media_type, generated_output, JobStatus.successful

    def execute_process(
            self,
            process_id: str,
            data_dict: dict,
            execution_mode: Optional[RequestedProcessExecutionMode] = None,
            requested_outputs: Optional[dict] = None,
            subscriber: Optional[Subscriber] = None,
            requested_response: Optional[RequestedResponse] = RequestedResponse.raw.value
    ) -> tuple[str, Any, JobStatus, Optional[dict[str, str]]]:
        """pygeoapi compatibility method.

        Contrary to pygeoapi, which stores requested execution parameters as
        a plain dictionary, pygeoapi-prefect rather uses a
        `schemas.ExecuteRequest` instance instead - this allows parsing the
        input data with the pydantic models crafted from the OGC API -
        Processes schemas. Thus, this method performs a light validation of the
        input data, converts it from a dict to a proper ExecuteRequest and
        forwards it to the `_execute` method, where execution is handled.
        Finally, it receives whatever results are generated and converts
        back to the data structure expected by pygeoapi.

        Also, note that current versions of pygeoapi only pass the `inputs`
        property of the execute request to the process manager. Therefore it
        is not possible to respond to additional execution request parameters,
        even if pygeoapi-prefect does support them.

        This means that, for the moment, pygeoapi does not pass other keys in
        the OAPIP `execute.yaml` schema, which are:

        - outputs
        - response
        - subscriber

        for more on this see:

        https://github.com/geopython/pygeoapi/issues/1285

        """
        # ToDo: properly implement the additional arguments
        #  requested_outputs, subscriber and requested_response
        # execution_request = ExecuteRequest(**data_dict)
        # this can raise a pydantic validation error
        execution_request = ExecuteRequest(inputs=data_dict)
        logger.warning(f"{data_dict=}")
        logger.warning(f"{execution_request=}")

        # Add ownership information to the request
        try:
            execution_request.properties["user"] = g.user
            execution_request.properties["roles"] = g.roles
        except AttributeError as err:
            logger.warning(err)

        execution_result = self._execute(
            process_id=process_id,
            execution_request=execution_request,
            requested_execution_mode=execution_mode,
        )
        (
            job_id,
            output_media_type,
            generated_output,
            status,
            additional_headers
        ) = execution_result
        return (
            job_id,
            output_media_type,
            generated_output,
            status,
            additional_headers,
        )

    def _execute(
            self,
            process_id: str,
            execution_request: ExecuteRequest,
            requested_execution_mode: RequestedProcessExecutionMode | None = None,
    ) -> tuple[str, str, Any, JobStatus, dict[str, str]]:
        """Process execution handler.

        This manager is able to execute two types of processes:

        - Normal pygeoapi processes, i.e. those that derive from
          `pygeoapi.process.base.BaseProcessor`. These are made into prefect flows
          and are run with prefect. These always run locally.

        - Custom prefect-aware processes, which derive from
          `pygeoapi_prefect.processes.base.BasePrefectProcessor`. These are able to take
          full advantage of prefect's features, which includes running elsewhere, as
          defined by deployments.
        """
        processor = self.get_processor(process_id)
        chosen_mode, additional_headers = self._select_execution_mode(
            requested_execution_mode, processor
        )
        job_id = str(uuid.uuid4())
        if isinstance(processor, BasePrefectProcessor):
            job_status = self._execute_prefect_processor(
                job_id, processor, chosen_mode, execution_request
            )
            # result = job_status.generated_outputs.get("result")
            output_media_type = ""
            generated_output = ""
            current_job_status = job_status.status
        else:
            output_media_type, generated_output, current_job_status = (
                self._execute_base_processor(job_id, processor, execution_request)
            )
        # return job_status, additional_headers
        return (
            job_id,
            output_media_type,
            generated_output,
            current_job_status,
            additional_headers
        )

    def get_job_result(self, job_id: str) -> Tuple[str, Any]:
        job = self.get_job_internal(job_id)
        # load from files
        return job_id, {k: m.model_dump() for k, m in job.generated_outputs.items()}

    # def get_output_data_raw(
    #         self, generated_output: OutputExecutionResultInternal, process_id: str
    # ) -> bytes:
    #     """Get output data as bytes."""
    #     processor = self.get_processor(process_id)
    #     if isinstance(processor, BasePrefectProcessor):
    #         if (sb := processor.result_storage_block) is not None:
    #             file_system = Block.load(sb)
    #             result = file_system.read_path(generated_output.location)
    #         else:
    #             result = super().get_output_data_raw(generated_output, process_id)
    #     else:
    #         result = super().get_output_data_raw(generated_output, process_id)
    #     return result
    #
    # def get_output_data_link_href(
    #         self, generated_output: OutputExecutionResultInternal, process_id: str
    # ) -> str:
    #     # we need to convert internal location into a proper href for a link
    #     return super().get_output_data_link_href(generated_output, process_id)

    def _flow_run_to_job_status(
            self, flow_run: FlowRun, prefect_flow: Flow
    ) -> JobStatusInfoInternal:
        job_id = self._flow_run_name_to_job_id(flow_run.name)
        generated_outputs = None
        try:
            partial_info = flow_run.state.result(raise_on_failure=False)
            if partial_info is not None:
                if isinstance(partial_info, tuple):
                    generated_outputs = partial_info[0].generated_outputs
                else:
                    generated_outputs = partial_info.generated_outputs
        except (MissingResult, UnfinishedRun, AttributeError) as err:
            logger.warning(f"Could not get flow_run results: {err}")

        execution_request = ExecuteRequest.model_construct(**flow_run.parameters["execution_request"])
        return JobStatusInfoInternal(
            jobID=job_id,
            status=self.prefect_state_map[flow_run.state_type],
            message=flow_run.state.message,
            processID=prefect_flow.name,
            created=flow_run.created,
            started=flow_run.start_time,
            finished=flow_run.end_time,
            requested_response_type=execution_request.response,
            requested_outputs=execution_request.outputs,
            generated_outputs=generated_outputs,
        )


async def _get_prefect_flow_runs(
        states: list[StateType] | None = None, name_like: str | None = None
) -> list[FlowRun]:
    """Retrieve existing prefect flow_runs, optionally filtered by state and name"""
    if states is not None:
        state_filter = filters.FlowRunFilterState(
            type=filters.FlowRunFilterStateType(any_=states)
        )
    else:
        state_filter = None
    if name_like is not None:
        name_like_filter = filters.FlowRunFilterName(like_=name_like)
    else:
        name_like_filter = None
    async with get_client() as client:
        response = await client.read_flow_runs(
            flow_run_filter=filters.FlowRunFilter(
                state=state_filter,
                name=name_like_filter,
            )
        )
    return response


async def _get_prefect_flow_run(flow_run_name: str) -> tuple[FlowRun, Flow] | None:
    """Retrieve prefect flow_run details."""
    async with get_client() as client:
        flow_runs = await client.read_flow_runs(
            flow_run_filter=filters.FlowRunFilter(
                name=filters.FlowRunFilterName(any_=[flow_run_name])
            )
        )
        try:
            flow_run = flow_runs[0]
        except IndexError:
            result = None
        else:
            prefect_flow = await client.read_flow(flow_run.flow_id)
            result = flow_run, prefect_flow
        return result


async def _get_prefect_flow(flow_id: uuid.UUID) -> Flow:
    """Retrive prefect flow details."""
    async with get_client() as client:
        return await client.read_flow(flow_id)
