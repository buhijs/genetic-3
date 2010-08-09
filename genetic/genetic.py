import sys
import string
import glob
import os
import time
import random as r
import signal

import atpy
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as mpl
import multiprocessing as mp
import subprocess

try:
    from mpi4py import MPI
    mpi_enabled = True
    comm = MPI.COMM_WORLD
    rank = comm.Get_rank()
    nproc = comm.Get_size()
    status = MPI.Status()
except:
    mpi_enabled = False
    rank = 0
    nproc = 1


def kill_all(ppid):

    pids = os.popen("ps -x -e -o pid,ppid | awk '{if($2 == "+str(ppid)+") print $1}'").read().split()

    if type(pids) <> list:
        pids = (pids, )

    for pid in pids:
        pid = pid.strip()
        if pid:
            pid = int(pid)
            kill_all(pid)

    try:
        os.kill(ppid, signal.SIGKILL)
        print "PID: ", ppid, " killed"
    except:
        print "PID: ", ppid, " does not exist"

    return


def time_waster():
    time.sleep(1000)


def etime(pid):
    cols = os.popen('ps -e -o pid,etime | grep '+str(pid)).read().strip().split()
    if len(cols) == 0:
        return 0
    else:
        cols = cols[1].split(':')
        if len(cols) == 2:
            d = 0
            h = 0
            m, s = cols
        elif len(cols) == 3:
            h, m, s = cols
            if '-' in h:
                d, h = h.split('-')
            else:
                d = 0
        elif len(cols) == 4:
            d, h, m, s = cols
        else:
            raise Exception("Can't understand "+str(cols))

        d = float(d)
        h = float(h)
        m = float(m)
        s = float(s)

        return ((d*24+h)*60+m)*60+s


def kill_inactive(seconds):
    for p in mp.active_children():
        if etime(p.pid) > seconds:
            print "Process %i has exceeded %i seconds, terminating" % (p.pid, seconds)
            kill_all(p.pid)


def wait_with_timeout(p, seconds):
    while True:
        if not p in mp.active_children():
            break
        if etime(p.pid) > seconds:
            print "Process %i has exceeded %i seconds, terminating" % (p.pid, seconds)
            kill_all(p.pid)
        time.sleep(0.1)

def create_dir(dir_name):
    delete_dir(dir_name)
    os.system("mkdir "+dir_name)


def delete_dir(dir_name):
    if os.path.exists(dir_name):
        reply = raw_input("Delete directory "+dir_name+"? [y/[n]] ")
        if reply=='y':
            os.system('rm -r '+dir_name)
        else:
            print "Aborting..."
            sys.exit()


# Tournament selection routine
# Good parameters for getting ~10% are k_frac=0.2 and p=0.9

def select(chi2, n, k_frac, p):

    k = int(len(chi2) * k_frac)

    assert k > 0, "k_frac is too small"

    model_id = [i for i in range(len(chi2))]

    prob = [p*(1-p)**j for j in range(k)]
    norm = sum(prob)
    for i in range(len(prob)):
        prob[i] = prob[i] / norm

    choices = []

    for t in range(n):

        pool_id = r.sample(model_id, k)

        pool_chi = chi2[pool_id]

        aux_list = zip(pool_chi, pool_id)
        aux_list.sort()
        pool_chi, pool_id = map(list, zip(*aux_list))

        xi = r.random()
        for j in range(k):
            if(xi <= sum(prob[0: j+1])): # is +1 because prob[0: 0] is empty
                choice = pool_id[j]
                choices.append(choice)
                break

    return(choices)


class Genetic(object):

    def __init__(self, n_models, output_dir, template, configuration, existing=False, fraction_output=0.1, fraction_mutation=0.5, n_cores=8, max_time=600, mpi=False):
        '''
        The Genetic class is used to control the SED fitter genetic algorithm

        Required Arguments:

            *n_models*: [ integer ]
                Number of models to run in the first generation, and to keep
                in subsequent generations

            *output_dir*: [ string ]
                The directory in which to output all the models

            *template*: [ string ]
                The template parameter file

            *configuration*: [ string ]

                The configuration file that describes how the parameters
                should be sampled. This file should contain four columns:

                    * The name of the parameter (no spaces)
                    * Whether to sample linearly ('linear') or logarithmically
                      ('log')
                    * The minimum value of the range
                    * The maximum value of the range

        Optional Arguments:

            *existing*: [ True | False ]
                Whether to keep any existing model directory

            *fraction_output*: [ float ]
                Fraction of models to add to and remove from the pool at each generation

            *fraction_mutation*: [ float ]
                Fraction of children that are mutations (vs crossovers)

            *n_cores*: [ integer ]
                Number of cores that can be used to compute models

            *max_time*: [ float ]
                Maximum number of seconds a model can run for

            *mpi*: [ bool ]
                Whether or not to run the models using MPI. If this is used,
                the number of cores is set by mpirun, not the n_cores option.
        '''

        # Read in parameters
        config_file = file(configuration)
        self.n_models = n_models
        self.models_dir = output_dir
        if mpi and not mpi_enabled:
            raise("Can't use MPI, mpi4py did not import correctly")
        else:
            self.mpi = mpi

        # Read in template parameter file
        self._template = file(template, 'r').readlines()

        # Read in configuration file
        self.parameters = {}
        for line in file(configuration, 'rb'):
            if not line.strip() == "":
                name, mode, vmin, vmax = string.split(line)
                self.parameters[name] = {'mode': mode, 'min': float(vmin),
                                         'max': float(vmax)}

        # Create output directory
        if not existing and (not self.mpi or rank==0):
            create_dir(self.models_dir)

        # Set genetic parameters
        self._fraction_output = fraction_output
        self._fraction_mutation = fraction_mutation
        self._n_cores = n_cores
        self._max_time = max_time

    def _generation_dir(self, generation):
        return self.models_dir + '/g%05i/' % generation

    def _parameter_file(self, generation, model_name):
        return self._parameter_dir(generation) + str(model_name) + '.par'

    def _model_prefix(self, generation, model_name):
        return self._model_dir(generation) + str(model_name)

    def _parameter_table(self, generation):
        return self._generation_dir(generation) + 'parameters.fits'

    def _fitting_results_file(self, generation):
        return self._generation_dir(generation) + 'fitting_output.fits'

    def _log_file(self, generation):
        return self._generation_dir(generation) + 'parameters.log'

    def _sampling_plot_file(self, generation):
        return self._generation_dir(generation) + 'sampling.eps'

    def _model_dir(self, generation):
        return self._generation_dir(generation) + 'models/'

    def _parameter_dir(self, generation):
        return self._generation_dir(generation) + 'par/'

    def _plots_dir(self, generation):
        return self._generation_dir(generation) + 'plots/'

    def initialize(self, generation):
        '''
        Initialize the directory structure for the generation specified.
        '''
        if not self.mpi or rank == 0:
            create_dir(self._generation_dir(generation))
            create_dir(self._model_dir(generation))
            create_dir(self._parameter_dir(generation))
            create_dir(self._plots_dir(generation))
        return

    def make_par_table(self, generation):
        '''
        Creates a table of models to compute for the generation specified.

        If this is the first generation, then the parameters are sampled in an
        unbiased way within the ranges specified by the user. Otherwise, this
        method uses results from previous generations to determine which
        models to run.
        '''
        if not self.mpi or rank == 0:

            print "[genetic] Generation %i: making parameter table" % generation

            t = atpy.Table()

            if generation==1:

                print "Initializing parameter file for first generation"

                # Create model names column
                t.add_column('model_name', ["g1_"+str(i) for i in range(self.n_models)], dtype='|S30')

                # Create table values
                for par_name in self.parameters:

                    mode = self.parameters[par_name]['mode']
                    vmin = self.parameters[par_name]['min']
                    vmax = self.parameters[par_name]['max']

                    if mode == "linear":
                        values = np.random.uniform(vmin, vmax, self.n_models)
                    elif mode == "log":
                        values = 10.**np.random.uniform(np.log10(vmin), np.log10(vmax), self.n_models)
                    else:
                        raise Exception("Unknown mode: %s" % mode)

                    t.add_column(par_name, values)

            else:

                n_output = int(self.n_models * self._fraction_output)

                # Create model names column
                t.add_column('model_name', ["g%i_%i" % (generation, i) for i in range(n_output)], dtype='|S30')

                # Read in previous parameter tables

                par_table = atpy.Table(self._parameter_table(1), verbose=False)
                for g in range(2, generation):
                    par_table.append(atpy.Table(self._parameter_table(g), verbose=False))

                model_names = np.array([x.strip() for x in par_table.model_name.tolist()])

                for column in par_table.names:
                    if column <> 'model_name':
                        t.add_empty_column(column, par_table.columns[column].dtype)

                # Read in fitter results, and sort from best to worst-fit chi^2

                chi2_table = atpy.Table(self._fitting_results_file(1), verbose=False)
                for g in range(2, generation):
                    chi2_table.append(atpy.Table(self._fitting_results_file(g), verbose=False))

                chi2_table.sort('chi2')

                order = np.argsort(chi2_table.chi2)

                # Truncate the table to the n_models first models

                # chi2_table = chi2_table.rows(range(self.n_models))
                chi2_table = chi2_table.rows(order[: self.n_models])

                print "Best fit so far: ", chi2_table.data[0]

                selected = []

                mutations = 0
                crossovers = 0

                logfile = file(self._log_file(generation), 'wb')

                for i in range(0, n_output):

                    # Select whether to do crossover or mutation

                    if(r.random() > self._fraction_mutation):

                        crossovers += 1

                        im1, im2 = select(chi2_table.chi2, n=2, k_frac=0.1, p=0.9)

                        m1 = chi2_table.model_name[im1]
                        m2 = chi2_table.model_name[im2]

                        logfile.write('g%s_%i = crossover of %s and %s\n' % (generation, i, m1, m2))

                        selected.append(im1)
                        selected.append(im2)

                        par_m1 = par_table.row(np.char.strip(par_table.model_name) == m1.strip())
                        par_m2 = par_table.row(np.char.strip(par_table.model_name) == m2.strip())

                        for par_name in par_table.names:

                            if par_name <> 'model_name':

                                par1 = par_m1[par_name]
                                par2 = par_m2[par_name]

                                xi = r.uniform(0., 1.)

                                mode = self.parameters[par_name]['mode']

                                if mode == "linear":
                                    value = par1 * xi + par2 * (1.-xi)
                                elif mode == "log":
                                    value = 10.**(np.log10(par1) * xi + np.log10(par2) * (1.-xi))
                                else:
                                    raise Exception("Unknown mode: %s" % mode)

                                t.data[par_name][i] = value

                    else:

                        mutations += 1

                        im1 = select(chi2_table.chi2, n=1, k_frac=0.1, p=0.9)[0]

                        m1 = chi2_table.model_name[im1]

                        logfile.write('g%s_%i = mutation of %s\n' % (generation, i, m1))

                        selected.append(im1)

                        mutation = r.choice(par_table.names)

                        par_m1 = par_table.row(np.char.strip(par_table.model_name) == m1.strip())

                        for par_name in par_table.names:

                            if par_name <> 'model_name':

                                value = par_m1[par_name]

                                if par_name == mutation:

                                    mode = self.parameters[par_name]['mode']
                                    vmin = self.parameters[par_name]['min']
                                    vmax = self.parameters[par_name]['max']

                                    if mode == "linear":
                                        value = r.uniform(vmin, vmax)
                                    elif mode == "log":
                                        value = 10.**r.uniform(np.log10(vmin), np.log10(vmax))
                                    else:
                                        raise Exception("Unknown mode: %s" % mode)

                                t.data[par_name][i] = value

                logfile.close()

                print "   Mutations  : "+str(mutations)
                print "   Crossovers : "+str(crossovers)

                fig = mpl.figure()
                ax = fig.add_subplot(111)
                ax.hist(selected, 50)
                fig.savefig(self._sampling_plot_file(generation))

            t.write(self._parameter_table(generation), verbose=False)

        return

    def make_par_indiv(self, generation, parser, interpreter=None):
        '''
        For the generation specified, will read in the parameters.fits file
        and the parameter file template, and will output individual parameter
        files to the par/ directory for that given generation.

        The parser argument should be used to pass a function that given a
        line from the parameter file will return the parameter name.

        Optionally, one can specify an interpreting function that given a
        parameter name and a dictionary of parameter values, will determine
        the actual value to use (useful for example if several parameters are
        correlated).
        '''

        if not self.mpi or rank == 0:

            print "[genetic] Generation %i: making individual parameter files" % generation

            # Read in table and construct dictionary
            table = atpy.Table(self._parameter_table(generation))
            par_table = [dict(zip(table.names, table.row(i)))
                            for i in range(len(table))]

            # Cycle through models and create a parameter file for each
            for model in par_table:

                # Create new parameter file
                model_name = model['model_name'].strip()
                f = file(self._parameter_file(generation, model_name), 'wb')

                # Cycle through template lines
                for line in self._template:
                    if "VAR" in line:
                        name = parser(line)
                        if interpreter:
                            value = interpreter(generation, name, model)
                        else:
                            value = model[name]
                        f.write(line.replace('VAR', str(value)))
                    else:
                        f.write(line)

                f.close()

        return

    def compute_models(self, generation, model):
        '''
        For the generation specified, will compute all the models listed in
        the par/ directory.

        The model argument should be used to pass a function that given a
        parameter file and an output model directory will compute the model
        for that input and produce output with the specified prefix.
        '''

        start_dir = os.path.abspath(".")

        if not self.mpi:

            print "[genetic] Generation %i: computing models on single node (no MPI)" % generation

            for par_file in glob.glob(os.path.join(self._parameter_dir(generation), '*.par')):

                # Prepare model name and output filename
                model_name = string.split(os.path.basename(par_file), '.')[0]

                # Wait until there are n_cores or less active threads
                while len(mp.active_children()) >= self._n_cores:
                    kill_inactive(self._max_time)
                    time.sleep(0.1)

                # Prepare thread
                os.chdir(start_dir)
                p = mp.Process(target=model.run, args=(par_file, self._model_dir(generation), model_name))

                # Start thread
                p.start()

                time.sleep(0.1)

            while len(mp.active_children()) > 0:
                kill_inactive(self._max_time)
                time.sleep(10)

        else:

            comm.barrier()

            if rank == 0:

                print "[genetic] Generation %i: computing models with %i processes (using MPI)" % (generation, nproc)

                for par_file in glob.glob(os.path.join(self._parameter_dir(generation), '*.par')):

                    print "[mpi] rank 0 waiting for communications"

                    while True:
                        comm.Iprobe(source=MPI.ANY_SOURCE, tag=1, status=status)
                        if status.source > 0:
                            break
                        time.sleep(0.1)

                    data = comm.recv(source=MPI.ANY_SOURCE, tag=1)
                    if data['status'] == 'ready':
                        print "[mpi] rank 0 received ready from rank %i" % data['source']
                        print "[mpi] rank 0 sending model %s to rank %i" % (par_file, data['source'])
                        comm.send({'model': par_file}, dest=data['source'], tag=2)
                    else:
                        raise Exception("Got unexpected status: %s" % data['status'])

                print "[mpi] rank 0 sending stop to all nodes"
                for dest in range(1, nproc):
                    comm.send({'model': 'stop'}, dest=dest, tag=2)

            else:

                while True:

                    comm.send({'status': 'ready', 'source': rank}, dest=0, tag=1)
                    data = comm.recv(source=0, tag=2)

                    if data['model'] == 'stop':
                        print "[mpi] rank %i has finished running models" % rank
                        break

                    par_file = data['model']

                    print "[mpi] rank %i running model: %s" % (rank, par_file)

                    # Prepare model name and output filename
                    model_name = string.split(os.path.basename(par_file), '.')[0]

                    # Prepare thread
                    os.chdir(start_dir)
                    p = mp.Process(target=model.run, args=(par_file, self._model_dir(generation), model_name))
                    p.start()
                    wait_with_timeout(p, self._max_time)

            comm.barrier()

    def compute_fits(self, generation, fitter):
        '''
        For the generation specified, will compute the fit of all the models.

        The fitter argument should be used to pass a function that given a
        directory containing all the models, an output file, and a directory
        that can be used for plots, will output a table containing at least
        two columns named 'model_name' and 'chi2'.
        '''
        if not self.mpi or rank == 0:
            print "[genetic] Generation %i: fitting and plotting" % generation
            fitter.run(self._model_dir(generation), self._fitting_results_file(generation), self._plots_dir(generation))
        return