import json
import os
from time import sleep
import threading

from docker import DockerClient
from docker.errors import NotFound, APIError, ContainerError
from json import JSONDecodeError

from riptide.config.document.config import Config
from riptide.config.document.service import Service
from riptide.config.service.ports import find_open_port_starting_at

from riptide_engine_docker.assets import riptide_engine_docker_assets_dir
from riptide_engine_docker.labels import RIPTIDE_DOCKER_LABEL_IS_RIPTIDE, RIPTIDE_DOCKER_LABEL_SERVICE, \
    RIPTIDE_DOCKER_LABEL_PROJECT, RIPTIDE_DOCKER_LABEL_MAIN, RIPTIDE_DOCKER_LABEL_HTTP_PORT
from riptide_engine_docker.mounts import create_mounts
from riptide_engine_docker.network import get_network_name
from riptide.engine.results import ResultQueue, ResultError, StartStopResultStep
from riptide.lib.cross_platform.cpuser import getuid, getgid

NO_START_STEPS = 6

ENTRYPOINT_CONTAINER_PATH = '/entrypoint_riptide.sh'

EENV_DONT_RUN_CMD = "RIPTIDE__DOCKER_DONT_RUN_CMD"
EENV_USER = "RIPTIDE__DOCKER_USER"
EENV_GROUP = "RIPTIDE__DOCKER_GROUP"
EENV_RUN_MAIN_CMD_AS_USER = "RIPTIDE__DOCKER_RUN_MAIN_CMD_AS_USER"
EENV_ORIGINAL_ENTRYPOINT = "RIPTIDE__DOCKER_ORIGINAL_ENTRYPOINT"
EENV_COMMAND_LOG_PREFIX = "RIPTIDE__DOCKER_CMD_LOGGING_"
EENV_NO_STDOUT_REDIRECT = "RIPTIDE__DOCKER_NO_STDOUT_REDIRECT"

# For services map HTTP main port to a host port starting here
DOCKER_ENGINE_HTTP_PORT_BND_START = 30000

start_lock = threading.Lock()


def start(project_name: str, service: Service, client: DockerClient, queue: ResultQueue):
    """
    Starts the given service by starting the container (if not already started).

    Finishes when service was successfully started or an error occured.
    Updates the ResultQueue with status messages for this service, as specified by ResultStart.
    If an error during start occurs, an ResultError is added to the queue, indicating the kind of error.
    On errors, tries to execute stop after updating the queue.


    :param client:          Docker Client
    :param project_name:    Name of the project to start
    :param service:         Service object defining the service
    :param queue:           ResultQueue to update, or None
    """
    # TODO: FG start
    # TODO: Function is way to long
    user = getuid()
    user_group = getgid()

    name = get_container_name(project_name, service["$name"])
    needs_to_be_started = False

    # 1. Check if already running
    queue.put(StartStopResultStep(current_step=1, steps=None, text='Checking...'))
    try:
        container = client.containers.get(name)
        if container.status != "running":
            container.remove()
            needs_to_be_started = True
    except NotFound:
        needs_to_be_started = True
    except APIError as err:
        queue.end_with_error(ResultError("ERROR checking container status.", cause=err))
        stop(project_name, service["$name"], client)
        return

    if needs_to_be_started:

        # 2. Pulling image
        queue.put(StartStopResultStep(current_step=2, steps=NO_START_STEPS, text="Checking image... "))
        # Check if image exists
        try:
            client.images.get(service["image"])
        except NotFound:
            try:
                queue.put(StartStopResultStep(current_step=2, steps=NO_START_STEPS, text="Pulling image... "))
                image_name_full = service['image'] if ":" in service['image'] else service['image'] + ":latest"
                for line in client.api.pull(image_name_full, stream=True):
                    try:
                        status = json.loads(line)
                        if "progress" in status:
                            queue.put(StartStopResultStep(current_step=2, steps=NO_START_STEPS, text="Pulling image... " + status["status"] + " : " + status["progress"]))
                        else:
                            queue.put(StartStopResultStep(current_step=2, steps=NO_START_STEPS, text="Pulling image... " + status["status"]))
                    except JSONDecodeError:
                        queue.put(StartStopResultStep(current_step=2, steps=NO_START_STEPS, text="Pulling image... " + str(line)))
            except APIError as err:
                queue.end_with_error(ResultError("ERROR pulling image.", cause=err))
                stop(project_name, service["$name"], client)
                return

        # Collect labels
        labels = {
            RIPTIDE_DOCKER_LABEL_IS_RIPTIDE: '1',
            RIPTIDE_DOCKER_LABEL_PROJECT: project_name,
            RIPTIDE_DOCKER_LABEL_SERVICE: service["$name"],
            RIPTIDE_DOCKER_LABEL_MAIN: "0"
        }
        if "roles" in service and "main" in service["roles"]:
            labels[RIPTIDE_DOCKER_LABEL_MAIN] = "1"

        try:
            # Collect volumes
            volumes = service.collect_volumes()
            # Add custom entrypoint as volume
            entrypoint_script = os.path.join(riptide_engine_docker_assets_dir(), 'entrypoint.sh')
            volumes[entrypoint_script] = {'bind': ENTRYPOINT_CONTAINER_PATH, 'mode': 'ro'}
            mounts = create_mounts(volumes)

            # Collect environment variables
            environment = service.collect_environment()
            # The original entrypoint of the image is replaced with
            # this custom entrypoint script, which may call the original entrypoint
            # if present
            # This adds configuration for this to the script.
            image_config = client.api.inspect_image(service["image"])["Config"]
            environment.update(parse_entrypoint(image_config["Entrypoint"]))
            # All command logging commands are added as environment variables for the
            # riptide entrypoint
            if "logging" in service and "commands" in service["logging"]:
                for cmdname, command in service["logging"]["commands"].items():
                    environment[EENV_COMMAND_LOG_PREFIX + cmdname] = command

            # Collect (and process!) additional_ports
            ports = service.collect_ports()

            # Change user?
            user_param = None if service["run_as_root"] else user
            if user_param:
                environment[EENV_RUN_MAIN_CMD_AS_USER] = "yes"
            # user and group are always created in the container, but only if the above ENV is set,
            # the main cmd/entrypoint will be run as non-root
            if not service["dont_create_user"]:
                environment[EENV_USER] = user
                environment[EENV_GROUP] = user_group

            # If src role is set, change workdir
            workdir = service.get_working_directory()
        except Exception as ex:
            queue.end_with_error(ResultError("ERROR preparing container.", cause=ex))
            return

        # 3. Run pre start commands
        cmd_no = -1
        for cmd in service["pre_start"]:
            cmd_no = cmd_no + 1
            queue.put(StartStopResultStep(current_step=3, steps=NO_START_STEPS, text="Pre Start: " + cmd))
            try:
                # Remove first, just to be sure
                try:
                    client.containers.get(name + "__pre_start" + str(cmd_no)).stop()
                except APIError:
                    pass
                try:
                    client.containers.get(name + "__pre_start" + str(cmd_no)).remove()
                except APIError:
                    pass

                # TODO: Keyboard interrupt support
                client.containers.run(
                    image=service["image"],
                    entrypoint=["/bin/sh", "-c", cmd],
                    detach=False,
                    remove=True,
                    name=name + "__pre_start" + str(cmd_no),
                    user=user_param,
                    group_add=[user_group],
                    volumes=volumes,
                    environment=environment,
                    ports=ports,
                    network=get_network_name(project_name),
                    labels={RIPTIDE_DOCKER_LABEL_IS_RIPTIDE: '1'},
                    working_dir=workdir
                )
            except (APIError, ContainerError) as err:
                queue.end_with_error(ResultError("ERROR running pre start command '" + cmd + "'.", cause=err))
                stop(project_name, service["$name"], client)
                return

        # 4. Starting the container
        queue.put(StartStopResultStep(current_step=4, steps=NO_START_STEPS, text="Starting Container..."))

        try:
            # Lock here to prevent race conditions with port assignments and possibly other stuff
            with start_lock:

                # Get port to bind main HTTP port to
                if "port" in service:
                    main_port = find_open_port_starting_at(DOCKER_ENGINE_HTTP_PORT_BND_START)
                    labels[RIPTIDE_DOCKER_LABEL_HTTP_PORT] = str(main_port)
                    ports[service["port"]] = main_port

                container = client.containers.run(
                    image=service["image"],
                    entrypoint=[ENTRYPOINT_CONTAINER_PATH],
                    command=image_config["Cmd"] if "command" not in service else service["command"],
                    detach=True,
                    name=name,
                    # user is always root, but EENV_USER may be used to run command with another user using the entrypoint
                    group_add=[user_group],
                    hostname=service["$name"],
                    labels=labels,
                    mounts=mounts,
                    environment=environment,
                    ports=ports,
                    working_dir=workdir
                )
                # Add container to network
                client.networks.get(get_network_name(project_name)).connect(container, aliases=[service["$name"]])
        except (APIError, ContainerError) as err:
            queue.end_with_error(ResultError("ERROR starting container.", cause=err))
            return

        # 4b. Checking if it actually started or just crashed immediately
        queue.put(StartStopResultStep(current_step=4, steps=NO_START_STEPS, text="Checking..."))
        sleep(3)
        try:
            container = client.containers.get(name)
            if container.status == "exited":
                extra = " Try 'run_as_root': true" if user_param else ""
                queue.end_with_error(ResultError("ERROR: Container crashed." + extra, details=container.logs().decode("utf-8")))
                container.remove()
                return
        except NotFound:
            queue.end_with_error(ResultError("ERROR: Container went missing."))
            return

        # 5. Execute Post Start commands via docker exec.
        cmd_no = -1
        for cmd in service["post_start"]:
            cmd_no = cmd_no + 1
            queue.put(StartStopResultStep(current_step=5, steps=NO_START_STEPS, text="Post Start: " + cmd))
            try:
                # TODO: Keyboard interrupt support (shutdown then!)
                container.exec_run(
                    cmd=["/bin/sh", "-c", cmd],
                    detach=False,
                    tty=True,
                    user=user_param
                )
            except (APIError, ContainerError) as err:
                queue.end_with_error(ResultError("ERROR running post start command '" + cmd + "'.", cause=err))
                stop(project_name, service["$name"], client)
                return

        # 6. Done!
        queue.put(StartStopResultStep(current_step=6, steps=NO_START_STEPS, text="Started!"))
    else:
        queue.put(StartStopResultStep(current_step=2, steps=2, text='Already started!'))
    queue.end()


def stop(project_name: str, service_name: str, client: DockerClient, queue: ResultQueue=None):
    """
    Stops the given service by stopping the container (if not already started).

    Finishes when service was successfully stopped or an error occured.
    Updates the ResultQueue with status messages for this service, as specified by ResultStop.
    If an error during stop occurs, an ResultError is added to the queue, indicating the kind of error.

    The queue is optional.

    :param project_name:    Name of the project to start
    :param service_name:    Name of the service to start
    :param queue:           ResultQueue to update, or None
    """
    name = get_container_name(project_name, service_name)
    # 1. Check if already running
    if queue:
        queue.put(StartStopResultStep(current_step=1, steps=None, text='Checking...'))
    try:
        container = client.containers.get(name)
        # 2. Stop
        if queue:
            queue.put(StartStopResultStep(current_step=2, steps=3, text='Stopping...'))
        container.stop()
        container.remove()
        if queue:
            queue.put(StartStopResultStep(current_step=3, steps=3, text='Stopped!'))
    except NotFound:
        if queue:
            queue.put(StartStopResultStep(current_step=2, steps=2, text='Already stopped!'))
    except APIError as err:
        if queue:
            queue.end_with_error(ResultError("ERROR checking container status.", cause=err))
        return

    if queue:
        queue.end()


def status(project_name: str, service: Service, client: DockerClient, system_config: Config):
    # Get Container
    name = get_container_name(project_name, service["$name"])
    container_is_running = False
    try:
        container = client.containers.get(name)
        if container.status != "exited":
            container_is_running = True
    except NotFound:
        pass

    return container_is_running


def get_container_name(project_name: str, service_name: str):
    return 'riptide__' + project_name + '__' + service_name


def parse_entrypoint(entrypoint):
    """
    Parse the original entrypoint of an image and return a map of variables for the riptide entrypoint script.
    RIPTIDE__DOCKER_ORIGINAL_ENTRYPOINT: Original entrypoint as string to be used with exec.
                                         Empty if original entrypoint is not set.
    RIPTIDE__DOCKER_DONT_RUN_CMD:        true or unset.
                                         When the original entrypoint is a string, the command does not get run.
                                         See table at https://docs.docker.com/engine/reference/builder/#shell-form-entrypoint-example
    """
    # Is the original entrypoint set?
    if not entrypoint:
        return {EENV_ORIGINAL_ENTRYPOINT: ""}
    # Is the original entrypoint shell or exec format?
    if isinstance(entrypoint, list):
        # exec format
        # Turn the list into a string, but quote all arguments
        command = entrypoint.pop(0)
        arguments = " ".join(['"%s"' % entry for entry in entrypoint])
        return {
            EENV_ORIGINAL_ENTRYPOINT: command + " " + arguments
        }
    else:
        # shell format
        return {
            EENV_ORIGINAL_ENTRYPOINT: "/bin/sh -c " + entrypoint,
            EENV_DONT_RUN_CMD: "true"
        }
    pass
