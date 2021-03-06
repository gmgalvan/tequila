import numpy, typing, numbers
from tequila.objective import Objective
from tequila.objective.objective import Variable, format_variable_dictionary
from .optimizer_base import Optimizer, OptimizerResults, dataclass
from tequila.circuit.noise import NoiseModel
from tequila.tools.qng import get_qng_combos, CallableVector, QNGVector
from tequila.utils import TequilaException

@dataclass
class GDResults(OptimizerResults):

    moments: dict = None

class OptimizerGD(Optimizer):
    """
    The gradient descent optimizer for tequila.

    OptimizerGD allows for two modalities: it can either function as a 'stepper', simply calculating updated
    parameter values for a given object; or it can be called to perform an entire optimization. The former
    is used to accomplish the latter, and can give users a more fine-grained control of the optimization.
    See Optimizer for details on inherited attributes or methods; there are several.


    Attributes
    ---------
    f:
        function which performs an optimization step.
    gradient_lookup:
        dictionary mapping object ids as strings to said object's callable gradient
    active_key_lookup:
        dictionary mapping object ids as strings to said object's active keys, itself a dict, of variables to optimize.
    moments_lookup:
        dictionary mapping object ids as strings to said object's current stored moments; a pair of lists of floats,
        namely running tallies of gradient momenta. said momenta are used to SCALE or REDIRECT gradient descent steps.
    moments_trajectory:
        dictionary mapping object ids as strings to said object's momenta at ALL steps; that is, a list of all
        the moments of a given object, in order.
    step_lookup:
        dictionary mapping object ids as strings to an int; how many optimization steps have been performed for
        a given object. Relevant only to the Adam optimizer.
    lr:
        a float. Hyperparameter: The learning rate (unscaled) to be used in each update;
        in some literature, called a step size.
    beta:
        a float. Hyperparameter: scales (perhaps nonlinearly) all first moment terms in any relavant method.
    rho:
        a float. Hyperparameter: scales (perhaps nonlinearly) all second moment terms in any relavant method.
        in some literature, may be referred to as 'beta_2'.
    epsilon:
        a float. Hyperparameter: used to prevent division by zero in some methods.
    tol:
        a float. If specified, __call__ aborts when the difference in energies between two steps is smaller than tol.


    Methods
    -------
    prepare:
        perform all necessary compilation and registration of a given objective. Must be called before step
        is used on the given optimizer.
    step:
        perform a single optimization step on a compiled objective, starting from a given point.
    reset_stepper:
        wipe all stored information about all prepared objectives.
    reset_momenta:
        reset all moment information about all prepared objectives, but do not erase compiled gradients.
    reset_momenta_for:
        reset all moment information about a given objective, but do not erase compiled gradients.


    """
    @classmethod
    def available_methods(cls):
        """:return: All tested available methods"""
        return ['adam', 'adagrad', 'adamax', 'nadam', 'sgd', 'momentum', 'nesterov', 'rmsprop', 'rmsprop-nesterov']

    def __init__(self, maxiter=100,
                 method='sgd',
                 tol: numbers.Real = None,
                 lr: numbers.Real = 0.1,
                 beta: numbers.Real = 0.9,
                 rho: numbers.Real = 0.999,
                 epsilon: numbers.Real = 1.0 * 10 ** (-7),
                 backend=None,
                 samples=None,
                 device=None,
                 noise=None,
                 silent=True,
                 **kwargs):

        """

        Parameters
        ----------
        maxiter: int: Default = 100:
            maximum number of iterations to perform, if using __call__ method.
        method: str: Default = 'sgd':
            string specifying which of the available methods to use for optimization. if not specified,
            then unmodified, stochastic gradient descent will be used.
        tol: numbers.Real, optional:
            if specified a tolerance that specifies when to deem that an optimization has converged.
            If None: no convergence criterion specified; __call__ runs till maxiter is reached. Must be positive, >0.
        lr:  numbers.Real: Default = 0.1:
            the learning rate to use. Rescales all steps; used by every optimizer.
            Default value is 0.1; chosen by fiat.
        beta: numbers.Real: Default = 0.9
            rescaling parameter for first moments in a number of methods. Must obey 0<beta<1.
            Default value suggested by original adam paper.
        rho: numbers.Real: Default = 0.999
            rescaling parameter for second moments in a number of methods. Must obey 0<beta<1.
            Default value suggested by original adam paper.
        epsilon: numbers.Real: Default = 10^-7:
            a float for prevention of division by zero in methods like adam. Must be positive.
            Default value suggested by original adam paper.
        backend: str, optional:
            a quantum backend to use. None means autopick.
        samples: int, optional:
            number of samples to simulate measurement of objectives with.
            Default: none, i.e full wavefunction simulation.
        device: optional:
            changeable type. The device on which to perform (or, simulate performing) actual quantum computation.
            Default None will use the basic, un-restricted simulators of backend.
        noise: optional:
            NoiseModel object or str 'device', being either a custom noisemodel or the instruction to use that of
            the emulated device.
            Default value none means: simulate without any noise.
        silent: bool: Default = False:
            suppresses printout during calls if True.
        kwargs
        """

        super().__init__(maxiter=maxiter, samples=samples,device=device,
                         backend=backend,silent=silent,
                         noise=noise,
                         **kwargs)
        method_dict = {
            'adam': self._adam,
            'adagrad': self._adagrad,
            'adamax': self._adamax,
            'nadam': self._nadam,
            'sgd': self._sgd,
            'momentum': self._momentum,
            'nesterov': self._nesterov,
            'rmsprop': self._rms,
            'rmsprop-nesterov': self._rms_nesterov}

        self.f = method_dict[method.lower()]
        self.gradient_lookup = {}
        self.active_key_lookup = {}
        self.moments_lookup = {}
        self.moments_trajectory = {}
        self.step_lookup = {}
        ### scaling parameters. lr is learning rate.
        ### beta rescales first moments. rho rescales second moments. epsilon is for division stability.
        self.lr = lr
        self.beta = beta
        self.rho = rho
        self.epsilon = epsilon
        assert all([k > .0 for k in [lr, beta, rho, epsilon]])
        self.tol = tol
        if self.tol is not None:
            self.tol = abs(float(tol))

    def __call__(self, objective: Objective,
                 maxiter: int = None,
                 initial_values: typing.Dict[Variable, numbers.Real] = None,
                 variables: typing.List[Variable] = None,
                 reset_history: bool = True,
                 method_options: dict = None,
                 gradient=None,
                 *args, **kwargs) -> GDResults:

        """
        perform a gradient descent optimization of an objective.

        Parameters
        ----------
        objective: Objective:
            the objective to optimize.
        maxiter: int, optional:
            Overrides the optimizer to specify maximum number of iterations to perform.
            Default value: use the maxiter supplied to __init__.
        initial_values: dict, optional:
            initial point at which to begin optimization.
            Default None: will be chosen randomly.
        variables: list, optional:
            which variables to optimize. Note that all variables not to be optimized must be specified in initial_values
            Default: optimize all variables of objective.
        reset_history: bool: Default = True:
            whether or not to wipe the self.history object.
        method_options: dict, optional:
            dummy keyword to play well with tq.minimize. Does nothing.
        gradient: optional:
            how to calculate gradients. if str '2-point', will use 2-point numerical gradients;
            if str 'qng' will use the default qng optimizer. Other more complex options possible.
        args
        kwargs

        Returns
        -------
        GDResults
            all the results of optimization.
        """


        if self.save_history and reset_history:
            self.reset_history()

        active_angles, passive_angles, variables = self.initialize_variables(objective, initial_values, variables)
        v = {**active_angles, **passive_angles}

        comp = self.prepare(objective=objective, initial_values=v, variables=variables, gradient=gradient)

        ### prefactor. Early stopping, initialization, etc. handled here

        if maxiter is None:
            maxiter = self.maxiter

        ### the actual algorithm acts here:
        e = comp(v, samples=self.samples)
        self.history.energies.append(e)
        self.history.angles.append(v)
        best = e
        best_angles = v
        v = self.step(comp, v)
        last = e
        for step in range(1, maxiter):
            e = comp(v, samples=self.samples)
            self.history.energies.append(e)
            self.history.angles.append(v)
            ### saving best performance
            if e < best:
                best = e
                best_angles = v

            if not self.silent:
                if self.print_level > 2:
                    string = "Iteration: {} , Energy: {:+2.8f}, angles: {}".format(str(step), e, v)
                else:
                    string = "Iteration: {} , Energy: {:+2.8f}".format(str(step), e)
                print(string)

            if self.tol != None:
                if numpy.abs(e - last) <= self.tol:
                    if not self.silent:
                        print('delta f smaller than tolerance {}. Stopping optimization.'.format(str(self.tol)))
                    break

            ### get new parameters with self.step!
            v = self.step(comp, v)
            last = e
        E_final, angles_final = best, best_angles
        return GDResults(energy=E_final, variables=format_variable_dictionary(angles_final), history=self.history,
                            moments=self.moments_trajectory[id(comp)])

    def prepare(self, objective: Objective, initial_values: dict = None,
                variables: list = None, gradient=None):
        """
        perform all initialization for an objective, register it with lookup tables, and return it compiled.
        MUST be called before step is used.

        Parameters
        ----------
        objective: Objective:
            the objective to ready for optimization.
        initial_values: dict, optional:
            the initial values of to prepare the optimizer with.
            Default: choose randomly.
        variables: list, optional:
            which variables to optimize over, and hence prepare gradients for.
            Default value: optimize over all variables in objective.
        gradient: optional:
            extra keyword; information used to compile alternate gradients.
            Default: prepare the standard, analytical gradient.

        Returns
        -------
        Objective:
            compiled version of objective.
        """

        active_angles, passive_angles, variables = self.initialize_variables(objective, initial_values, variables)
        comp = self.compile_objective(objective=objective)
        for arg in comp.args:
            if hasattr(arg,'U'):
                if arg.U.device is not None:
                    # don't retrieve computer 100 times; pyquil errors out if this happens!
                    self.device = arg.U.device
                    break


        compile_gradient = True

        dE = None
        if isinstance(gradient, str):
            if gradient.lower() == 'qng':
                compile_gradient = False

                combos = get_qng_combos(objective, initial_values=initial_values, backend=self.backend,
                                        device=self.device,
                                        samples=self.samples, noise=self.noise,
                                        )
                dE = QNGVector(combos)
            else:
                gradient = {"method": gradient, "stepsize": 1.e-4}

        elif isinstance(gradient,dict):
            if gradient['method'] == 'qng':
                func = gradient['function']
                compile_gradient = False
                combos = get_qng_combos(objective,func=func, initial_values=initial_values, backend=self.backend,
                                        device=self.device,
                                        samples=self.samples, noise=self.noise)
                dE = QNGVector(combos)

        if compile_gradient:
            grad_obj, comp_grad_obj = self.compile_gradient(objective=objective, variables=variables, gradient=gradient)
            dE = CallableVector([comp_grad_obj[k] for k in comp_grad_obj.keys()])

        ostring = id(comp)
        if not self.silent:
            print(self)
            print("{:15} : {} expectationvalues".format("Objective", objective.count_expectationvalues()))
            if compile_gradient:
                counts = [x.count_expectationvalues() for x in comp_grad_obj.values()]
                print("{:15} : {} expectationvalues".format("Gradient", sum(counts)))
                print("{:15} : {}".format("gradient instr", gradient))
            print("{:15} : {}".format("active variables", len(active_angles)))

        vec_len = len(active_angles)
        first = numpy.zeros(vec_len)
        second = numpy.zeros(vec_len)
        self.gradient_lookup[ostring] = dE
        self.active_key_lookup[ostring] = active_angles.keys()
        self.moments_lookup[ostring] = (first, second)
        self.moments_trajectory[ostring] = [(first, second)]
        self.step_lookup[ostring] = 0
        return comp

    def step(self, objective: Objective, parameters: typing.Dict[Variable, numbers.Real]) -> \
            typing.Dict[Variable, numbers.Real]:
        """
        perform a single optimization step and return suggested parameters.
        Parameters
        ----------
        objective: Objective:
            the compiled objective, to perform an optimization step for. MUST be one returned by prepare.
        parameters: dict:
            the parameters to use in performing the optimization step.

        Returns
        -------
        dict
            dict of new suggested parameters.
        """
        s = id(objective)
        try:
            gradients = self.gradient_lookup[s]
            active_keys = self.active_key_lookup[s]
            last_moment = self.moments_lookup[s]
            adam_step = self.step_lookup[s]
        except:
            raise TequilaException(
                'Could not retrieve necessary information. Please use the prepare function before optimizing!')
        new, moments, grads = self.f(step=adam_step, gradients=gradients, active_keys=active_keys, moments=last_moment,
                                     v=parameters)
        back = {**parameters}
        for k in new.keys():
            back[k] = new[k]
        save_grad = {}
        self.moments_lookup[s] = moments
        self.moments_trajectory[s].append(moments)
        if self.save_history:
            for i, k in enumerate(active_keys):
                save_grad[k] = grads[i]
            self.history.gradients.append(save_grad)
        self.step_lookup[s] += 1
        return back

    def reset_stepper(self):
        """
        reset all information about all prepared objectives.
        Returns
        -------
        None
        """
        self.moments_trajectory = {}
        self.moments_lookup = {}
        self.step_lookup = {}
        self.gradient_lookup = {}
        self.reset_history()

    def reset_momenta(self):
        """
        reset moment information about all prepared objectives.
        Returns
        -------
        None
        """
        for k in self.moments_lookup.keys():
            m = self.moments_lookup[k]
            vlen = len(m[0])
            first = numpy.zeros(vlen)
            second = numpy.zeros(vlen)
            self.moments_lookup[k] = (first, second)
            self.moments_trajectory[k] = [(first, second)]
            self.step_lookup[k] = 0

    def reset_momenta_for(self, objective: Objective):
        """
        reset moment information about a specific objective.
        Parameters
        ----------
        objective: Objective:
            the objective whose information should be reset.

        Returns
        -------
        None
        """
        k = id(objective)
        try:
            m = self.moments_lookup[k]
            vlen = len(m[0])
            first = numpy.zeros(vlen)
            second = numpy.zeros(vlen)
            self.moments_lookup[k] = (first, second)
            self.moments_trajectory[k] = [(first, second)]
            self.step_lookup[k] = 0
        except:
            print('found no compiled objective with id {} in lookup. Did you pass the correct object?'.format(k))

    def _adam(self, gradients, step,
              v, moments, active_keys,
              **kwargs):
        t = step + 1
        s = moments[0]
        r = moments[1]
        grads = gradients(v, samples=self.samples)
        s = self.beta * s + (1 - self.beta) * grads
        r = self.rho * r + (1 - self.rho) * numpy.square(grads)
        s_hat = s / (1 - self.beta ** t)
        r_hat = r / (1 - self.rho ** t)
        updates = []
        for i in range(len(grads)):
            rule = - self.lr * s_hat[i] / (numpy.sqrt(r_hat[i]) + self.epsilon)
            updates.append(rule)
        new = {}
        for i, k in enumerate(active_keys):
            new[k] = v[k] + updates[i]
        back_moment = [s, r]
        return new, back_moment, grads

    def _adagrad(self, gradients,
                 v, moments, active_keys, **kwargs):
        r = moments[1]
        grads = gradients(v, self.samples)

        r += numpy.square(grads)
        new = {}
        for i, k in enumerate(active_keys):
            new[k] = v[k] - self.lr * grads[i] / numpy.sqrt(r[i] + self.epsilon)

        back_moments = [moments[0], r]
        return new, back_moments, grads

    def _adamax(self, gradients,
                v, moments, active_keys, **kwargs):

        s = moments[0]
        r = moments[1]
        grads = gradients(v, samples=self.samples)
        s = self.beta * s + (1 - self.beta) * grads
        r = self.rho * r + (1 - self.rho) * numpy.linalg.norm(grads, numpy.inf)
        updates = []
        for i in range(len(grads)):
            rule = - self.lr * s[i] / r[i]
            updates.append(rule)
        new = {}
        for i, k in enumerate(active_keys):
            new[k] = v[k] + updates[i]
        back_moment = [s, r]
        return new, back_moment, grads

    def _nadam(self, step, gradients,
               v, moments, active_keys,
               **kwargs):

        s = moments[0]
        r = moments[1]
        t = step + 1
        grads = gradients(v, samples=self.samples)
        s = self.beta * s + (1 - self.beta) * grads
        r = self.rho * r + (1 - self.rho) * numpy.square(grads)
        s_hat = s / (1 - self.beta ** t)
        r_hat = r / (1 - self.rho ** t)
        updates = []
        for i in range(len(grads)):
            rule = - self.lr * (self.beta * s_hat[i] + (1 - self.beta) * grads[i] / (1 - self.beta ** t)) / (
                        numpy.sqrt(r_hat[i]) + self.epsilon)
            updates.append(rule)
        new = {}
        for i, k in enumerate(active_keys):
            new[k] = v[k] + updates[i]
        back_moment = [s, r]
        return new, back_moment, grads

    def _sgd(self, gradients,
             v, moments, active_keys, **kwargs):

        grads = gradients(v, samples=self.samples)
        new = {}
        for i, k in enumerate(active_keys):
            new[k] = v[k] - self.lr * grads[i]
        return new, moments, grads

    def _momentum(self, gradients,
                  v, moments, active_keys, **kwargs):

        m = moments[0]
        grads = gradients(v, samples=self.samples)

        m = self.beta * m - self.lr * grads
        new = {}
        for i, k in enumerate(active_keys):
            new[k] = v[k] + m[i]

        back_moments = [m, moments[1]]
        return new, back_moments, grads

    def _nesterov(self, gradients,
                  v, moments, active_keys, **kwargs):

        m = moments[0]

        interim = {}
        for i, k in enumerate(active_keys):
            interim[k] = v[k] + self.beta * m[i]
        active_keyset = set([k for k in active_keys])
        total_keyset = set([k for k in v.keys()])
        for k in total_keyset:
            if k not in active_keyset:
                interim[k] = v[k]
        grads = gradients(interim, samples=self.samples)

        m = self.beta * m - self.lr * grads
        new = {}
        for i, k in enumerate(active_keys):
            new[k] = v[k] + m[i]

        back_moments = [m, moments[1]]
        return new, back_moments, grads

    def _rms(self, gradients,
             v, moments, active_keys,
             **kwargs):

        r = moments[1]
        grads = gradients(v, samples=self.samples)
        r = self.rho * r + (1 - self.rho) * numpy.square(grads)
        new = {}
        for i, k in enumerate(active_keys):
            new[k] = v[k] - self.lr * grads[i] / numpy.sqrt(self.epsilon + r[i])

        back_moments = [moments[0], r]
        return new, back_moments, grads

    def _rms_nesterov(self, gradients,
                      v, moments, active_keys,
                      **kwargs):

        m = moments[0]
        r = moments[1]

        interim = {}
        for i, k in enumerate(active_keys):
            interim[k] = v[k] + self.beta * m[i]
        active_keyset = set([k for k in active_keys])
        total_keyset = set([k for k in v.keys()])
        for k in total_keyset:
            if k not in active_keyset:
                interim[k] = v[k]
        grads = gradients(interim, samples=self.samples)

        r = self.rho * r + (1 - self.rho) * numpy.square(grads)
        for i in range(len(m)):
            m[i] = self.beta * m[i] - self.lr * grads[i] / numpy.sqrt(r[i])
        new = {}
        for i, k in enumerate(active_keys):
            new[k] = v[k] + m[i]

        back_moments = [m, r]
        return new, back_moments, grads


def minimize(objective: Objective,
             lr=0.1,
             method='sgd',
             initial_values: typing.Dict[typing.Hashable, numbers.Real] = None,
             variables: typing.List[typing.Hashable] = None,
             gradient: str = None,
             samples: int = None,
             maxiter: int = 100,
             backend: str = None,
             noise: NoiseModel = None,
             device: str = None,
             tol: float = None,
             silent: bool = False,
             save_history: bool = True,
             beta: float = 0.9,
             rho: float = 0.999,
             epsilon: float = 1. * 10 ** (-7),
             *args,
             **kwargs) -> GDResults:

    """ Initialize and call the GD optimizer.
    Parameters
    ----------
    objective: Objective :
        The tequila objective to optimize
    lr: float >0:
        the learning rate. Default 0.1.
    beta: float >0:
        scaling factor for first moments. default 0.9
    rho: float >0:
        scaling factor for second moments. default 0.999
    epsilon: float>0:
        small float for stability of division. default 10^-7

    method: string: Default = 'sgd'
        which variation on Gradient Descent to use. Options include 'sgd','adam','nesterov','adagrad','rmsprop', etc.
    initial_values: typing.Dict[typing.Hashable, numbers.Real], optional:
        Initial values as dictionary of Hashable types (variable keys) and floating point numbers. If given None,
         they will all be set to zero
    variables: typing.List[typing.Hashable], optional:
         List of Variables to optimize
    gradient: optional:
        the gradient to use. If None, calculated in the usual way. if str='qng', then the qng is calculated.
        If a dictionary of objectives, those objectives are used. If another dictionary,
        an attempt will be made to interpret that dictionary to get, say, numerical gradients.
    samples: int, optional:
         samples/shots to take in every run of the quantum circuits (None activates full wavefunction simulation)
    maxiter: int : Default = 100:
         the maximum number of iterations to run.
    backend: str, optional:
         Simulation backend which will be automatically chosen if set to None
    noise: NoiseModel, optional:
         a NoiseModel to apply to all expectation values in the objective.
    device: optional:
        the device from which to (potentially, simulatedly) sample all quantum circuits employed in optimization.
    tol: float : Default = 10^-4
         Convergence tolerance for optimization; if abs(delta f) smaller than tol, stop.
    silent: bool : Default = False:
         No printout if True
    save_history: bool: Default = True:
        Save the history throughout the optimization

    Note
    ----

    optional kwargs may include beta, beta2, and rho, parameters which affect
    (but do not need to be altered) the various method algorithms.

    Returns
    -------
    GDResults:
        the results of an optimization.

    """
    if isinstance(gradient, dict) or hasattr(gradient, "items"):
        if all([isinstance(x, Objective) for x in gradient.values()]):
            gradient = format_variable_dictionary(gradient)
    optimizer = OptimizerGD(save_history=save_history,
                            method=method,
                            lr=lr,
                            beta=beta,
                            rho=rho,
                            tol=tol,
                            epsilon=epsilon,
                            samples=samples, backend=backend,
                            device=device,
                            noise=noise,
                            maxiter=maxiter,
                            silent=silent)
    return optimizer(objective=objective,
                     maxiter=maxiter,
                     gradient=gradient,
                     initial_values=initial_values,
                     variables=variables, *args, **kwargs)
