import numpy as np
import jax
import jax.numpy as jnp
from jax.scipy.linalg import expm
import diffrax

from bounded_while_loop import bounded_optimize

smallest_float32 = jnp.finfo('float32').smallest_normal

def product(x,axis=None,keepdims=False):
    return jnp.exp(jnp.sum(safe_log(x),axis=axis,keepdims=keepdims))

def offdiag_mask (N):
    return jnp.ones((N,N)) - jnp.eye(N)

def safe_log (x):
    return jnp.log (jnp.maximum (x, smallest_float32))

def safe_recip (x):
    return 1 / jnp.maximum (x, smallest_float32)

def symmetrise (matrix):
    return (matrix + matrix.swapaxes(-1,-2)) / 2

def row_normalise (matrix):
    return matrix - jnp.diag(jnp.sum(matrix, axis=-1))

def round_up_to_power (x, base=2):
    if base == 2:  # avoid doubling length due to precision errors
        x = 1 << (x-1).bit_length()
    else:
        x = int (jnp.ceil (base ** jnp.ceil (jnp.log(x) / jnp.log(base))))
    return x

def logsumexp (x, axis=None, keepdims=False):
    max_x = jnp.max (x, axis=axis, keepdims=keepdims)
    return jnp.log(jnp.sum(jnp.exp(x - max_x), axis=axis, keepdims=keepdims)) + max_x

def replace_nth (old_arr, n, new_nth_elt):
    return [jax.lax.cond (k==n, lambda x:new_nth_elt, lambda x:x, old_kth_elt) for k, old_kth_elt in enumerate(old_arr)]
    
# Implements the algorithm from Cohn et al (2010), for protein Potts models parameterized by contact & coupling matrices
# Cohn et al (2010), JMLR 11:93. Mean Field Variational Approximation for Continuous-Time Bayesian Networks

# Model:
#  N = number of states (amino acids)
#  S = symmetric exchangeability matrix (N*N). For i!=j, S_ij=S_ji=rate of substitution from i<->j if i and j are equiprobable. S_ii = -sum_j S_ij
#  J = symmetric coupling matrix (N*N). For i!=j, J_ij=J_ji=interaction strength between components i and j. J_ii = 0
#  h = bias vector (N). h_i=bias of state i

# Data-dependent aspects of model:
#  K = # of components, each having N states (the sequence length)
#  C = symmetric binary contact matrix (K*K). For i!=j, C_ij=C_ji=1 if i and j are in contact, 0 otherwise. C_ii=0

# Since C is a sparse matrix, with each component having at most M<<K neighbors, we represent it compactly as follows:
#  nbr_idx = sparse neighbor matrix (M*L). nbr_idx[i,n] is the index of the n-th neighbor of component i
#  nbr_mask = sparse neighbor flag matrix (M*L). nbr_mask[i,n] is 1 if nbr_idx[i,n] is a real neighbor, 0 otherwise

def normalise_ctbn_params (params):
    return { 'S' : symmetrise(row_normalise(jnp.abs(params['S']))),
             'J' : symmetrise(params['J']),
             'h' : params['h'] }

# Rate for substitution x_i->y_i conditioned on neighboring x's
#  i = 1..K
#  x = (K,) vector of integers from 0..N-1
#  y_i = integer from 0..N-1
def q_k (i, x, y_i, nbr_idx, nbr_mask, params):
    S = params['S']
    J = params['J']
    h = params['h']
    return S[x[i],y_i] * jnp.exp (h[y_i] + 2*jnp.dot (nbr_mask[i], J[y_i,x[nbr_idx[i]]]))

# Endpoint-conditioned variational approximation:
#  mu = (K,N) matrix of mean-field probabilities
#  rho = (K,N) matrix where entry (i,x_i) is the probability of reaching the final state given that component #i is in state x_i

# Mean-field averaged rates for a continuous-time Bayesian network
# Returns (A,N,N) matrix where entry (a,x_{idx[a]},y_{idx[a]}) is mean-field averaged rate matrix for component idx[a]
def q_bar (idx, nbr_idx, nbr_mask, params, mu):
    N = mu.shape[-1]
    S = params['S']
    J = params['J']
    h = params['h']
    exp_2J = jnp.exp (2 * J)  # (y_i,x_k)
    exp_2JC = exp_2J[None,None,:,:]  # (a,k,y_i,x_{nbr_k})
    mu_nbr = mu[nbr_idx[idx]]  # (a,k,x_{nbr_k})
    mu_exp_2JC = jnp.einsum ('akx,akyx->aky', mu_nbr, exp_2JC) ** nbr_mask[idx][:,:,None]  # (a,k,y_i)
#    jax.debug.print("exp_2JC={exp_2JC} mu_nbr={mu_nbr} mu_exp_2JC={mu_exp_2JC} nbr_mask[idx]={nm}",mu_exp_2JC=mu_exp_2JC,exp_2JC=exp_2JC,mu_nbr=mu_nbr,nm=nbr_mask[idx])
    S = S * offdiag_mask(N)
    return S[None,:,:] * jnp.exp(h)[None,None,:] * product(mu_exp_2JC,axis=-2,keepdims=True)  # (a,x_i,y_i)

# Returns (M,N,N,N) tensor where entry (j,x_j,x_i,y_i) is the mean-field averaged rate x_i->y_i, conditioned on component nbr_idx[i,j] being in state x_{nbr_idx[i,j]}
# NB only valid for x_i != y_i
def q_bar_cond (i, nbr_idx, nbr_mask, params, mu):
    M = nbr_idx.shape[-1]
    N = mu.shape[-1]
    S = params['S']
    J = params['J']
    h = params['h']
    nonself_nbr_mask = offdiag_mask(M) * jnp.outer(nbr_mask[i],nbr_mask[i])  # (j,k)
    cond_energy = nbr_mask[i,:,None,None] * J[None,:,:]  # (j,x_{nbr_j},y_i)
    exp_2J = jnp.exp (2 * J)  # (N,N)
    exp_2JC = exp_2J[None,None,:,:] ** nonself_nbr_mask[:,:,None,None]  # (j,k,y_i,x_{nbr_k})
    mu_nbr = mu[nbr_idx[i]] ** nbr_mask[i,:]  # (k,x_{nbr_k})
    mu_exp_JC = jnp.einsum ('kx,jkyx->jky', mu_nbr, exp_2JC)  # (j,k,y_i)
    S = S * offdiag_mask(N)
    return S[None,None,:,:] * jnp.exp(h)[None,None,None,:] * jnp.exp(-2*cond_energy)[:,:,None,:] * product(mu_exp_JC,axis=-2)[:,None,None,:]  # (j,x_{nbr_j},x_i,y_i)

# Geometrically-averaged mean-field rates for a continuous-time Bayesian network
# Returns (A,N,N) matrix where entry (a,x_{idx[a]},y_{idx[a]}) is geometrically-averaged mean-field rate matrix for component idx[a]
# NB only valid for x_i != y_i
def q_tilde (idx, nbr_idx, nbr_mask, params, mu):
    N = mu.shape[-1]
    S = params['S']
    J = params['J']
    h = params['h']
    mean_energy = jnp.einsum ('akx,ak,yx->ay', mu[nbr_idx[idx,:]], nbr_mask[idx,:], J)  # (a,y_i)
    S = S * offdiag_mask(N)
    return S[None,:,:] * jnp.exp(h+2*mean_energy)[:,None,:]  # (a,x_i,y_i)

# Returns (M,N,N,N) matrix where entry (j,x_j,x_i,y_i) is the geometrically-averaged rate x_i->y_i, conditioned on component nbr_idx[i,j] being in state x_{nbr_idx[i,j]}
# NB only valid for x_i != y_i
def q_tilde_cond (i, nbr_idx, nbr_mask, params, mu):
    M = nbr_idx.shape[-1]
    N = mu.shape[-1]
    S = params['S']
    J = params['J']
    h = params['h']
    nonself_nbr_mask = offdiag_mask(M) * jnp.outer(nbr_mask[i],nbr_mask[i])  # (j,k)
    cond_energy = nbr_mask[i,:,None,None] * J[None,:,:]  # (j,x_{nbr_j},y_i)
    mean_energy = jnp.einsum ('kx,jk,yx->jy', mu[nbr_idx[i]], nonself_nbr_mask, J)  # (j,y_i)
    S = S * offdiag_mask(N)
    return S[None,None,:,:] * jnp.exp(h)[None,None,None,:] * jnp.exp(2*cond_energy)[:,:,None,:] * jnp.exp(2*mean_energy)[:,None,None,:]  # (j,x_{nbr_j},x_i,y_i)

# Rate matrix for a single component, q_{xy} = S_{xy}
# S: (N,N)
# h: (N,)
def q_single_offdiag (params):
    S = params['S']
    h = params['h']
    N = S.shape[0]
    return S * offdiag_mask(N) * jnp.exp(h)[None,:]

def q_single (params):
    return row_normalise (q_single_offdiag (params))

# Amalgamated (joint) rate matrix for all components
# Note this is big: (N^K,N^K)
def q_joint (nbr_idx, nbr_mask, params):
    N = params['S'].shape[0]
    K,M = nbr_idx.shape
    def get_components (x):
        return idx_to_seq (x, N, K)
    def get_rate (xs, ys):
        diffs = jnp.where (xs == ys, 0, 1)
        i = jnp.argmax (diffs)
        return jnp.where (jnp.sum(diffs) == 1, q_k(i, xs, ys[i], nbr_idx, nbr_mask, params), 0)
    states = jax.vmap (get_components)(jnp.arange(N**K))
    Q = jax.vmap (lambda x: jax.vmap (lambda y: get_rate(x,y))(states))(states)
    return row_normalise(Q)

def idx_to_seq (idx, N, K):
    return jnp.array ([idx // (N**j) % N for j in range(K)])
                       
def seq_to_idx (seq, N):
    return jnp.sum (jnp.array ([seq[j] * (N**j) for j in range(seq.shape[0])]))

def all_seqs (N, K):
    return [jnp.array(X) for X in np.ndindex(tuple([N]*K))]

# Returns (A,N,N) matrix where entry (k,x_{idx[a]},y_{idx[a]}) is the joint probability of transition x_{idx[a]}->y_{idx[a]} for component idx[a]
def gamma (idx, nbr_idx, nbr_mask, params, mu, rho):
    g = jnp.einsum ('ax,axy,ay,ax->axy', mu[idx], q_tilde(idx,nbr_idx,nbr_mask,params,mu), rho[idx], safe_recip(rho[idx]))
#    jax.debug.print('mu[idx]={mu} qtilde={qt} rho[idx]={rho} g={g}',mu=mu[idx],rho=rho[idx],qt=q_tilde(idx,nbr_idx,nbr_mask,params,mu),g=g)
    return g

# Returns (N,) vector
def psi (i, nbr_idx, nbr_mask, params, mu, rho):
    gammas = gamma(nbr_idx[i], nbr_idx, nbr_mask, params, mu, rho)  # (M,N,N)
    qbar_cond = q_bar_cond(i,nbr_idx,nbr_mask,params,mu)  # (M,N,N,N)
    qtilde_cond = q_tilde_cond(i,nbr_idx,nbr_mask,params,mu)  # (M,N,N,N)
    log_qtilde_cond = safe_log (jnp.where (qtilde_cond < 0, 1, qtilde_cond))  # (M,N,N,N)
#    jax.debug.print ("i={i} gammas={g} mu[nbr_idx[i]]={mu} qbar_cond={qb} qtilde_cond={qt} log_qtilde_cond={lq}", i=i, g=gammas, mu=mu[nbr_idx[i]], qb=qbar_cond, qt=qtilde_cond, lq=log_qtilde_cond)
    return -jnp.einsum('jy,jxyz,j->x',mu[nbr_idx[i]],qbar_cond,nbr_mask[i]) + jnp.einsum('jyz,jxyz,j->x',gammas,log_qtilde_cond,nbr_mask[i])

def rho_deriv (i, nbr_idx, nbr_mask, params, mu, rho):
    K = mu.shape[0]
    qbar = q_bar(jnp.array([i]), nbr_idx, nbr_mask, params, mu)[0,:,:]  # (N,N)
    qbar_diag = -jnp.einsum ('xy->x', qbar)  # (N,)
    _psi = psi(i, nbr_idx, nbr_mask, params, mu, rho)  # (N,)
    qtilde = q_tilde(jnp.array([i]), nbr_idx, nbr_mask, params, mu)  # (1,N,N)
    rho_deriv_i = -rho[i,:] * (qbar_diag + _psi) - jnp.einsum ('y,xy->x', rho[i,:], qtilde[0,:,:])  # (N,)
#    jax.debug.print ("qbar={qb} qbar_diag={qd} qtilde={qt} psi={psi} rho_deriv={deriv}", qb=qbar, qd=qbar_diag, qt=qtilde, psi=_psi, deriv=rho_deriv_i)
    return rho_deriv_i

def mu_deriv (i, nbr_idx, nbr_mask, params, mu, rho):
    K = mu.shape[0]
    _gamma = gamma(jnp.array([i]), nbr_idx, nbr_mask, params, mu, rho)[0,:,:]  # (N,N)
    mu_deriv_i = jnp.einsum('yx->x',_gamma) - jnp.einsum('xy->x',_gamma)  # (N,)
    return mu_deriv_i

def F_deriv (seq_mask, nbr_idx, nbr_mask, params, mu, rho):
    K, N = mu.shape
    idx = jnp.arange(K)
    qbar = q_bar(idx, nbr_idx, nbr_mask, params, mu)  # (K,N,N)
    qtilde = q_tilde(idx, nbr_idx, nbr_mask, params, mu)  # (K,N,N)
    _gamma = gamma (idx, nbr_idx, nbr_mask, params, mu, rho)  # (K,N,N)
    mask = seq_mask[:,None,None] * offdiag_mask(N)[None,:,:]  # (K,N,N)
    log_qtilde = safe_log(jnp.where(mask,qtilde,1))
    gamma_coeff = log_qtilde + 1 + safe_log(mu)[:,:,None] - safe_log(_gamma)
    dF = -jnp.einsum('ix,ixy,ixy->',mu,qbar,mask) + jnp.einsum('ixy,ixy,ixy->',_gamma,gamma_coeff,mask)
#    jax.debug.print("mu={mu} rho={rho} dF={dF} qbar={qbar} gamma={gamma} gamma_coeff={gamma_coeff}", mu=mu, rho=rho, dF=dF, qbar=qbar, gamma=_gamma, gamma_coeff=gamma_coeff)
    return dF

# Exact equilibrium distribution for a complete rate matrix
def exact_eqm (q):
    N = q.shape[0]
    evals, evecs_r = jnp.linalg.eig(q)
    evals = jnp.abs(evals)
    min_eval = jnp.min(evals)  # the eigenvalue closest to zero can be assumed to correspond to the equilibrium
    min_eval_idx = jnp.where(jnp.isclose(evals,min_eval))[0]
    assert min_eval_idx.size == 1, f"Can't handle orthogonal equilibria: evals {evals[min_eval_idx]}"
    evecs_l = jnp.linalg.inv(evecs_r)
    eqm = jnp.real(evecs_l[min_eval_idx[0],:])
    eqm /= jnp.sum(eqm)
    return eqm

# Exact posterior for a complete rate matrix
class ExactRho:
    def __init__ (self, q, T, x, y):
        N = q.shape[0]
        assert q.shape == (N,N)
        self.N = N
        self.q = row_normalise(q)
        self.T = T
        self.x = x
        self.y = y
        self.exp_qT = expm(q*T) [x, y]

    def evaluate (self, t):
        rho = expm(self.q*(self.T-t)) [:, self.y]  # (N,)
        return jnp.minimum(rho,1)

class ExactMu (ExactRho):
    def evaluate (self, t):
        rho = super().evaluate (t)
        exp_qt = expm(self.q*t) [self.x, :]  # (N,)
        mu = exp_qt * rho / self.exp_qT
        return mu / jnp.sum(mu)

# Dummy class that returns fixed solution for mu and/or rho
class FixedSolution():
    def __init__ (self, val):
        self.val = val
    
    def evaluate (self, t):
        return self.val

class ZeroSolution(FixedSolution):
    def __init__ (self, N):
        super().__init__ (jnp.zeros(N))

# helper to evaluate mu and rho from arrays of Solution-like objects
def eval_mu_rho (mu_solns, rho_solns, t):
    mu = jnp.stack ([mu_soln.evaluate(t) for mu_soln in mu_solns])
    rho = jnp.stack ([rho_soln.evaluate(t) for rho_soln in rho_solns])
    return mu, rho

# wrappers for diffrax
def F_term (t, F_t, args):
    seq_mask, nbr_idx, nbr_mask, params, mu_solns, rho_solns, T = args
    mu, rho = eval_mu_rho (mu_solns, rho_solns, t)
    mu = jnp.where (t < T, mu, rho)  # guard against explosion at boundary
#    jax.debug.print ("t={t} mu={mu} rho={rho} dF={deriv}", t=t, mu=mu, rho=rho, deriv=F_deriv (seq_mask, nbr_idx, nbr_mask, params, mu, rho))
    return F_deriv (seq_mask, nbr_idx, nbr_mask, params, mu, rho)

def solve_F (seq_mask, nbr_idx, nbr_mask, params, mu_solns, rho_solns, T, rtol=1e-3, atol=1e-6):
    term = diffrax.ODETerm (F_term)
    solver = diffrax.Dopri5()
    controller = diffrax.PIDController (rtol=rtol, atol=atol)
    F_soln = diffrax.diffeqsolve (terms=term, solver=solver, t0=0, t1=T, dt0=None, y0=0,
                                  args=(seq_mask, nbr_idx, nbr_mask, params, mu_solns, rho_solns, T),
                                  stepsize_controller = controller)
    return F_soln.ys[-1]

def rho_term (t, rho_i_t, args):
    i, seq_mask, nbr_idx, nbr_mask, params, mu_solns, rho_solns, T = args
    mu, rho = eval_mu_rho (mu_solns, rho_solns, t)
    mu = jnp.where (t < T, mu, rho)  # guard against explosion at boundary
    old_rho_i_t = rho[i,:]
    rho = rho.at[i].set(rho_i_t)
    _rho_deriv = rho_deriv (i, nbr_idx, nbr_mask, params, mu, rho)
#    jax.debug.print ("t={t} old_rho[{i}]={old} new_rho[{i}]={new} deriv={deriv}\n mu={mu}\n rho={rho}", t=t, mu=mu, rho=rho, deriv=_rho_deriv, old=old_rho_i_t, new=rho_i_t, i=i)
    return seq_mask[i] * _rho_deriv

def solve_rho (i, rho_i_T, seq_mask, nbr_idx, nbr_mask, params, mu_solns, rho_solns, T, rtol=1e-3, atol=1e-6):
    term = diffrax.ODETerm (rho_term)
    solver = diffrax.Dopri5()
    controller = diffrax.PIDController (rtol=rtol, atol=atol)
    return diffrax.diffeqsolve (terms=term, solver=solver, t0=T, t1=0, dt0=None, y0 = rho_i_T,
                                args=(i, seq_mask, nbr_idx, nbr_mask, params, mu_solns, rho_solns, T),
                                stepsize_controller = controller,
                                saveat = diffrax.SaveAt(dense=True))

def mu_term (t, mu_i_t, args):
    i, seq_mask, nbr_idx, nbr_mask, params, mu_solns, rho_solns, T = args
    mu, rho = eval_mu_rho (mu_solns, rho_solns, t)
    mu = mu.at[i].set(mu_i_t)
    mu = jnp.where (t < T, mu, rho)  # guard against explosion at boundary
#    jax.debug.print ("t={t} mu={mu} rho={rho} gamma={g} deriv={deriv}", t=t, mu=mu, rho=rho, deriv=mu_deriv (i, nbr_idx, nbr_mask, params, mu, rho), g=gamma(jnp.array([i]), nbr_idx, nbr_mask, params, mu, rho)[0,:,:])
    return seq_mask[i] * mu_deriv (i, nbr_idx, nbr_mask, params, mu, rho)

def solve_mu (i, mu_i_0, seq_mask, nbr_idx, nbr_mask, params, mu_solns, rho_solns, T, rtol=1e-3, atol=1e-6):
    term = diffrax.ODETerm (mu_term)
    solver = diffrax.Dopri5()
    controller = diffrax.PIDController (rtol=rtol, atol=atol)
    return diffrax.diffeqsolve (terms=term, solver=solver, t0=0, t1=T, dt0=None, y0=mu_i_0,
                                args=(i, seq_mask, nbr_idx, nbr_mask, params, mu_solns, rho_solns, T),
                                stepsize_controller = controller,
                                saveat = diffrax.SaveAt(dense=True))

# Calculate the variational likelihood for an endpoint-conditioned continuous-time Bayesian network
# This is a strict lower bound for log P(X_T=ys|X_0=xs,T,params)
def ctbn_variational_log_cond (prng, xs, ys, seq_mask, nbr_idx, nbr_mask, params, T, min_inc=1e-3, max_updates=4096):
    K = nbr_idx.shape[0]
    N = params['S'].shape[0]
    params = normalise_ctbn_params (params)
    # create arrays of boundary conditions for mu(0) and rho(T)
    mu_0 = jnp.eye(N)[xs]
    rho_T = jnp.eye(N)[ys]
    # create initial mu, rho solutions by assuming no interactions
    q1 = q_single (params)
    init_rho_solns = [ExactRho (q1, T, xs[i], ys[i]) for i in range(K)]
    init_mu_solns = [ExactMu (q1, T, xs[i], ys[i]) for i in range(K)]
    # do one update so (rho_solns,mu_solns) are diffrax AbstractPath's, to keep types uniform inside while loop
    rho_solns = [solve_rho (i, rho_T[i,:], seq_mask, nbr_idx, nbr_mask, params, init_mu_solns, init_rho_solns, T) for i in range(K)]
    mu_solns = [solve_mu (i, mu_0[i,:], seq_mask, nbr_idx, nbr_mask, params, init_mu_solns, rho_solns, T) for i in range(K)]
    # while (F_current - F_prev)/F_prev > minimum relative increase:
    #  for component indices i, in (nonrepeating) random order:
    #   solve rho and then mu for component i, using diffrax, and replace single-component posteriors with diffrax Solution's
    #   F_prev <- F_current, F_current <- new variational bound
    # To avoid repetition, we have to propagate the last index visited through the outer while loop
    def score_fun (outer_state):
        mu_solns, rho_solns = outer_state[0]
        F = solve_F (seq_mask, nbr_idx, nbr_mask, params, mu_solns, rho_solns, T)
        return F
    def update_fun (outer_state):
        inner_state, last_i = outer_state
        order = jax.random.permutation (prng, K)
        order = jax.lax.cond (last_i == order[0], lambda x:order[::-1], lambda x:order, None)  # avoid repetition
#        jax.debug.print("order={order}",order=order)
        inner_state, _ = jax.lax.scan (loop_body_fun, inner_state, order)
        return inner_state, order[-1]
    def loop_body_fun (inner_state, i):
        mu_solns, rho_solns = inner_state
        new_rho_i = solve_rho (i, rho_T[i,:], seq_mask, nbr_idx, nbr_mask, params, mu_solns, rho_solns, T)
        rho_solns = replace_nth (rho_solns, i, new_rho_i)
        new_mu_i = solve_mu (i, mu_0[i,:], seq_mask, nbr_idx, nbr_mask, params, mu_solns, rho_solns, T)
        mu_solns = replace_nth (mu_solns, i, new_mu_i)
        return (mu_solns, rho_solns), None
    log_elbo, (mu_rho, _last_i)  = bounded_optimize(score_fun, update_fun, ((mu_solns, rho_solns), -1), max_updates, min_inc=min_inc)
    return log_elbo, mu_rho

# Given sequences xs,ys and contact matrix C, return padded xs,ys along with seq_mask,nbr_idx,nbr_mask
def get_Markov_blankets (C, xs=None, ys=None, K=None, M=None):
    K_prepad = C.shape[0]
    assert C.shape == (K_prepad,K_prepad), "C must be a square matrix"
    if xs is not None:
        assert len(xs) == K_prepad, "length of xs must equal size of C"
    if ys is not None:
        assert len(ys) == K_prepad, "length of ys must equal size of C"
    if K is None:
        K = round_up_to_power (K_prepad)
    nbr_idx_list = [C[i,:].nonzero()[0] for i in range(K_prepad)]
    if M is None:
        M = round_up_to_power (max ([len(nbr_idx) for nbr_idx in nbr_idx_list]))
    else:
        assert M >= max ([len(nbr_idx) for nbr_idx in nbr_idx_list]), "M must be at least as large as the largest number of neighbors"
    seq_idx = jnp.arange(K)
    seq_mask = jnp.where(seq_idx < K_prepad, 1, 0)
    nbr_mask = jnp.array ([[1] * len(nbr_idx) + [0] * (M - len(nbr_idx)) for nbr_idx in nbr_idx_list] + [[0] * M] * (K - K_prepad))
    nbr_idx = jnp.array ([jnp.concatenate([nbrs,jnp.zeros(M - len(nbrs),dtype=nbrs.dtype)]) for nbrs in nbr_idx_list])
    if xs is not None:
        xs = xs + [0] * (K - K_prepad)
    if ys is not None:
        ys = ys + [0] * (K - K_prepad)
    return seq_mask, nbr_idx, nbr_mask, xs, ys

# Weak L2 regularizer for J and h
def ctbn_param_regularizer (params, alpha=1e-4):
    return alpha * (jnp.sum (params['J']**2) + jnp.sum (params['h']**2))

# Log-pseudolikelihood for a continuous-time Bayesian network
def ctbn_pseudo_log_marg (xs, seq_mask, nbr_idx, nbr_mask, params):
    K = nbr_idx.shape[0]
    N = params['S'].shape[0]
    params = normalise_ctbn_params (params)
    E_iy = params['h'][None,:] + jnp.einsum('ijy,ij->iy',params['J'][nbr_idx,:],nbr_mask)  # (K,N)
    log_Zi = logsumexp (E_iy, axis=-1)  # (K,)
    L_i = E_iy[jnp.arange(K),xs] - log_Zi  # (K,N)
    return jnp.sum (L_i * seq_mask)

# Mean-field approximation to log partition function of continuous-time Bayesian network
def ctbn_mean_field_log_Z (seq_mask, nbr_idx, nbr_mask, params, theta):
    E = jnp.einsum('ix,x->',theta,params['h']) + jnp.einsum('ix,ijy,xy,ij->',theta,theta[nbr_idx,:],params['J'],nbr_mask)
    H = -jnp.einsum('ix->',theta * jnp.log(theta))
    return E + H

# Variational lower bound for log partition function of continuous-time Bayesian network
def ctbn_variational_log_Z (seq_mask, nbr_idx, nbr_mask, params, min_inc=1e-3, max_updates=4):
    K = nbr_idx.shape[0]
    N = params['S'].shape[0]
    params = normalise_ctbn_params (params)
    theta = jnp.repeat (jax.nn.softmax (params['h'])[None,:], K, axis=0)  # (K,N)
    def score_fun (theta):
        return ctbn_mean_field_log_Z (seq_mask, nbr_idx, nbr_mask, params, theta)
    def update_fun (theta):
        return jax.nn.softmax (params['h'][None,:] + 2 * jnp.einsum('ijy,xy,ij->ix',theta[nbr_idx,:],params['J'],nbr_mask))
    return bounded_optimize(score_fun, update_fun, theta, max_updates, min_inc=min_inc)

# Unnormalized log-marginal for a continuous-time Bayesian network
def ctbn_log_marg_unnorm (xs, seq_mask, nbr_idx, nbr_mask, params):
    K, M = nbr_idx.shape
    N = params['S'].shape[0]
    params = normalise_ctbn_params (params)
    E_i = params['h'][xs] + jnp.einsum('ij,ij->i',params['J'][jnp.repeat(xs[:,None],M,axis=-1),xs[nbr_idx]],nbr_mask)  # (K,)
    return jnp.sum (E_i * seq_mask)

# Variational log-marginal for a continuous-time Bayesian network
def ctbn_variational_log_marg (xs, seq_mask, nbr_idx, nbr_mask, params, log_Z = None):
    if log_Z is None:
        log_Z = ctbn_variational_log_Z (seq_mask, nbr_idx, nbr_mask, params)
    log_p = ctbn_log_marg_unnorm (xs, seq_mask, nbr_idx, nbr_mask, params)
    return log_p - log_Z

# Exact log-partition function for a continuous-time Bayesian network
def ctbn_exact_log_Z (seq_mask, nbr_idx, nbr_mask, params):
    K = nbr_idx.shape[0]
    N = params['S'].shape[0]
    params = normalise_ctbn_params (params)
    Xs = all_seqs(N,K)
    X_is_valid = jnp.array ([jnp.all(seq_mask * X == X) for X in Xs])
    Es = jnp.array ([ctbn_log_marg_unnorm(X,seq_mask,nbr_idx,nbr_mask,params) for X in Xs])
    return logsumexp(jnp.where(X_is_valid,Es,-jnp.inf)).item()

# Exact log-marginal for a continuous-time Bayesian network
def ctbn_exact_log_marg (xs, seq_mask, nbr_idx, nbr_mask, params, log_Z = None):
    if log_Z is None:
        log_Z = ctbn_exact_log_Z (seq_mask, nbr_idx, nbr_mask, params)
    log_p = ctbn_log_marg_unnorm (xs, seq_mask, nbr_idx, nbr_mask, params)
    return log_p - log_Z
