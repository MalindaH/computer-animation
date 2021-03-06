import taichi as ti
import numpy as np

ti.init(arch=ti.gpu)

n_particles, n_grid, n_lines = 10000, 128, 0
dx, inv_dx = 1 / n_grid, float(n_grid)
dt = 1e-4 
gravity = 70
p_vol, p_rho = (dx * 0.5)**2, 1
p_mass = p_vol * p_rho # particle mass
E, nu = 0.1e4, 0.2 # Young's modulus and Poisson's ratio
mu_0, lambda_0 = E / (2 * (1 + nu)), E * nu / ((1+nu) * (1 - 2 * nu)) # Lame parameters

x = ti.Vector.field(2, dtype=float, shape=n_particles) # particle position
v = ti.Vector.field(2, dtype=float, shape=n_particles) # particle velocity
C = ti.Matrix.field(2, 2, dtype=float, shape=n_particles) # affine velocity field
F = ti.Matrix.field(2, 2, dtype=float, shape=n_particles) # deformation gradient
material = ti.field(dtype=int, shape=n_particles) # material id: 0: fluid, 1: jelly, 2: snow
Jp = ti.field(dtype=float, shape=n_particles) # plastic deformation
grid_v = ti.Vector.field(2, dtype=float, shape=(n_grid, n_grid)) # grid node momentum/velocity
grid_m = ti.field(dtype=float, shape=(n_grid, n_grid)) # grid node mass

@ti.func
def P2G(): # Particle to grid (P2G)
    for p in x: 
        base = (x[p] * inv_dx - 0.5).cast(int)
        fx = x[p] * inv_dx - base.cast(float)
        # Bspline
        w = [0.5 * (1.5 - fx) ** 2, 0.75 - (fx - 1) ** 2, 0.5 * (fx - 0.5) ** 2]
        F[p] = (ti.Matrix.identity(float, 2) + dt * C[p]) @ F[p] # deformation gradient update
        h = max(0.1, min(5, ti.exp(10 * (1.0 - Jp[p])))) # Hardening coefficient
        if material[p] == 1: # jelly
            h = 0.3
        mu, la = mu_0 * h, lambda_0 * h
        if material[p] == 0: # liquid
            mu = 0.0
        U, sig, V = ti.svd(F[p])
        J = 1.0
        for d in ti.static(range(2)):
            new_sig = sig[d, d]
            if material[p] == 2: # snow
                new_sig = min(max(sig[d, d], 1 - 2.5e-2), 1 + 4.5e-3)  # plasticity
            Jp[p] *= sig[d, d] / new_sig
            sig[d, d] = new_sig
            J *= new_sig
        if material[p] == 0:  # Reset deformation gradient
            F[p] = ti.Matrix.identity(float, 2) * ti.sqrt(J)
        elif material[p] == 2:
            F[p] = U @ sig @ V.transpose() # Reconstruct elastic deformation gradient
        stress = 2 * mu * (F[p] - U @ V.transpose()) @ F[p].transpose() + ti.Matrix.identity(float, 2) * la * J * (J - 1)
        stress = (-dt * p_vol * 4 * inv_dx * inv_dx) * stress
        affine = stress + p_mass * C[p]
        for i, j in ti.static(ti.ndrange(3, 3)): # Loop over 3x3 grid
            offset = ti.Vector([i, j])
            dpos = (offset.cast(float) - fx) * dx
            weight = w[i][0] * w[j][1]
            grid_v[base + offset] += weight * (p_mass * v[p] + affine @ dpos)
            grid_m[base + offset] += weight * p_mass

@ti.func
def G2P(): # grid to particle (G2P)
    for p in x: 
        base = (x[p] * inv_dx - 0.5).cast(int)
        fx = x[p] * inv_dx - base.cast(float)
        w = [0.5 * (1.5 - fx) ** 2, 0.75 - (fx - 1.0) ** 2, 0.5 * (fx - 0.5) ** 2]
        v_pic = ti.Vector.zero(float, 2)
        C_pic = ti.Matrix.zero(float, 2, 2)
        for i, j in ti.static(ti.ndrange(3, 3)): # loop over 3x3 grid
            dpos = ti.Vector([i, j]).cast(float) - fx
            g_v = grid_v[base + ti.Vector([i, j])]
            weight = w[i][0] * w[j][1]
            v_pic += weight * g_v
            C_pic += 4 * inv_dx * weight * g_v.outer_product(dpos)
        v[p], C[p] = v_pic, C_pic
        x[p] += dt * v[p] # advection

@ti.func
def update_gridv():
    for i, j in grid_m:
        if grid_m[i, j] > 0:
            grid_v[i, j] = (1 / grid_m[i, j]) * grid_v[i, j] # momentum to velocity
            grid_v[i, j][1] -= dt * gravity

@ti.func
def enforce_boundary():
    for i, j in grid_m:
        # grid boundary
        if i < 3 and grid_v[i, j][0] < 0: grid_v[i, j][0] = 0
        if i > n_grid - 3 and grid_v[i, j][0] > 0: grid_v[i, j][0] = 0
        if j < 3 and grid_v[i, j][1] < 0: grid_v[i, j][1] = 0
        if j > n_grid - 3 and grid_v[i, j][1] > 0: grid_v[i, j][1] = 0

@ti.kernel
def substep():
    for i, j in grid_m:
        grid_v[i, j] = [0, 0]
        grid_m[i, j] = 0

    P2G()
    update_gridv()
    enforce_boundary()
    G2P()

@ti.kernel
def initialize():
  for i in range(n_particles):
    if i < n_particles*2/3:
      x[i] = [ti.random() * 0.3 + 0.35, ti.random() * 0.3 + 0.05 ]
      material[i] = 0 # 0: fluid, 1: jelly, 2: snow
    elif i < n_particles*5/6:
      x[i] = [ti.random() * 0.15 + 0.3, ti.random() * 0.15 + 0.45 ]
      material[i] = 1 
    else:
      x[i] = [ti.random() * 0.15 + 0.55, ti.random() * 0.15 + 0.6]
      material[i] = 2
    v[i] = ti.Matrix([0, 0])
    F[i] = ti.Matrix([[1, 0], [0, 1]])
    Jp[i] = 1

initialize()
gui = ti.GUI("Fluid MLS-MPM", res=512, background_color=0x000000)
while not gui.get_event(ti.GUI.ESCAPE, ti.GUI.EXIT):
    for s in range(int(2e-3 // dt)):
        substep()
    colors = np.array([0x36eeff, 0xfca13f, 0xEEEEF0], dtype=np.uint32)
    gui.circles(x.to_numpy(), radius=1.5, color=colors[material.to_numpy()])
    gui.show() # only show gui
    # gui.show(f'{gui.frame:06d}.png') # save image screenshots in current directory
