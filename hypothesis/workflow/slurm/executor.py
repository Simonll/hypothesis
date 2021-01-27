import cloudpickle as pickle
import hypothesis.workflow as w
import logging
import os
import shutil
import sys
import tempfile


def execute(context=None,
            directory='.',
            environment=None,
            partition=None,
            store=None,
            cleanup=False):
    # Create the generation directory
    if directory is None:
        directory = tempfile.mkdtemp()
    if not os.path.exists(directory):
        os.makedirs(directory)
    # Get the absolute path
    directory = os.path.abspath(directory)
    os.chdir(directory)
    tasks_directory = directory + "/tasks"
    if not os.path.exists(tasks_directory):
        os.makedirs(tasks_directory)
    # Check if a custom context has been specified
    if context is None:
        context = w.context
    # Prune the computational graph
    context.prune()
    # Check if a root node is present
    if context.root is None:
        logging.critical("Postconditions of computational graph are met. Nothing to do.")
        sys.exit(0)  # Nothing to do
    # Add default Slurm attributes to the nodes
    add_default_attributes(context, directory=directory)
    # Set the compute partition of the tasks.
    add_partition(context, partition=partition)
    # Set the default anaconda environment
    add_default_environment(context, environment=environment)
    # Generate the executables for the processor
    generate_executables(context, directory)
    # Generate the task files
    for node in context.nodes:
        generate_task_file(node, tasks_directory)
    # Generate the submission script
    lines = []
    lines.append("#!/usr/bin/env bash -i")
    lines.append("#")
    lines.append("# Slurm submission script, generated by Hypothesis.")
    lines.append("# github.com/montefiore-ai/hypothesis")
    lines.append("#")
    lines.append("mkdir -p " + directory + "/logging")
    # Retrieve the tasks in BFS order
    tasks = context.program()
    task_indices = {}
    # Generate the main tasks and their dependencies
    job_id_line = "echo \""
    for task_index, task in enumerate(tasks):
        task_indices[id(task)] = task_index
        variable = "t" + str(task_index)
        line = variable + "=$(sbatch "
        # Check if the task has dependencies
        if len(task.dependencies) > 0:
            flag = "--dependency=afterok"
            for dependency in task.dependencies:
                dependency_index = task_indices[id(dependency)]
                flag += ":$t" + str(dependency_index)
            line += flag + " "
        line += directory + "/tasks/" + task_filename(task) + ")"
        lines.append(line)
        job_id_line += '$' + variable + "\n"
    # Create a file containing all Slurm identifiers
    job_id_line += "\" > " + directory + "/slurm_jobs"
    lines.append(job_id_line)
    # Write the pipeline file
    pipeline_path = directory + "/pipeline.bash"
    with open(pipeline_path, "w") as f:
        for line in lines:
            f.write(line + "\n")
    # Execute the bash script
    os.system("bash " + pipeline_path)
    if store is not None and store != directory:
        shutil.copyfile(directory + "/slurm_jobs", store + "/slurm_jobs")
        os.remove(directory + "/slurm_jobs")
    # Cleanup the generated Slurm files.
    if cleanup:
        os.remove(pipeline_path)
        shutil.rmtree(tasks_directory)


def generate_executables(context, directory):
    for node in context.nodes:
        code = pickle.dumps(node.f)
        with open(directory + "/" + executable_name(node), "wb") as f:
            f.write(code)


def task_filename(node):
    return str(id(node)) + ".sbatch"


def executable_name(node):
    return str(id(node)) + ".code"


def add_default_environment(context, environment=None):
    if environment is None and "CONDA_DEFAULT_ENV" in os.environ:
        # Grab the anaconda environment defined through the terminal
        environment = os.environ["CONDA_DEFAULT_ENV"]
    # Set the environment on all compute nodes
    if environment is not None:
        for node in context.nodes:
            node["conda"] = environment



def add_partition(context, partition=None):
    if partition is not None:
        for node in context.nodes:
            node["--partition"] = partition


def add_default_attributes(context, directory=None):
    for node in context.nodes:
        node["--export"] = "ALL"  # Exports all environment variables,
        node["--parsable"] = ""   # Enables convenient reading of task ID.
        node["--requeue"] = ""    # Automatically requeue when something fails.
        if directory is not None:
            logging_directory = "logging/" + node.name
            if node.tasks > 1:
                fmt = logging_directory + "-%A_%a.log"
            else:
                fmt = logging_directory + "-%j.log"
            node["--output"] = fmt
            node["--chdir"] = directory


def generate_task_file(node, directory):
    lines = []
    lines.append("#!/usr/bin/env bash")
    lines.append("#")
    lines.append("#")
    lines.append("# Slurm arguments, generated by Hypothesis.")
    lines.append("# github.com/montefiore-ai/hypothesis")
    lines.append("#")
    # Check if a custom name has been specified.
    if "--job-name" not in node.attributes:
        node.attributes["--job-name"] = str(node)
    # Add the node attributes
    for key in node.attributes:
        if key[:2] != "--":  # Skip non SBATCH arguments
            continue
        value = node[key]
        line = "#SBATCH " + key
        if len(value) > 0:
            line += "=" + value
        lines.append(line)
    # Check if the tasks is an array tasks.
    if node.tasks > 1:
        multiarray = True
        lines.append("#SBATCH --array 0-" + str(node.tasks - 1))
    else:
        multiarray = False
    # Check if a custom Anaconda environment has been specified.
    try:
        environment = node["conda"]
        lines.append("eval \"$(conda shell.bash hook)\"")
        lines.append("conda activate " + environment)
    except:
        pass
    # Execute the function
    line = "python -u -m hypothesis.bin.workflow.processor " + executable_name(node)
    if multiarray:
        line += " $SLURM_ARRAY_TASK_ID"
    lines.append(line)
    # Write the task file.
    with open(directory + "/" + task_filename(node), "w") as f:
        for line in lines:
            f.write(line + "\n")
