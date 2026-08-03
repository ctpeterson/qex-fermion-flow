# Bilinear Fermion Flow

Contains snippets of Hadrons and QEX code for calculating flowed quark bilinears using fermion flow. In the following, I describe how everything works out for the symmetric Dirac operator, though the same is true for any quark bilinear. The Hadrons code calculates all sixteen quark bilinears from the sixteen unique Gamma structures in addition to the bilinear for the symmetric Dirac operator.

## Fermion flow of bilinears

What is to follow was first described in [Itou, E., Aoki, S. (arXiv:1701.08983)](https://arxiv.org/abs/1701.08983), to the best of my knowledge. Fermion flow takes

$$
\psi_{t}(y) = \sum_{x}\mathcal{K}_{t}(x \rightarrow y)\psi(x),
$$

where the kernel $\mathcal{K}_{t}$ is the Green's function:

$$
\big(\partial_{t} - \not{\mathcal{D}}^{2}\big)\mathcal{K}_{t}(x \rightarrow y) = \delta(t)\delta_{xy}.
$$

Now consider the bilinear

$$
\mathcal{E}(t) \equiv \sum_{x} \big\langle\overline{\psi}_{t}(x) \overleftrightarrow{\not{\mathcal{D}}} \psi_{t}(x)\big\rangle
$$

with

$$
\overline{\psi}_{t}(x) \overleftrightarrow{\not{\mathcal{D}}} \psi_{t}(x) \equiv \overline{\psi}_{t}(x) \Big(\overrightarrow{\not{\mathcal{D}}} - \overleftarrow{\not{\mathcal{D}}}\Big) \psi_{t}(x).
$$

This is essentially the fermionic analogue of the Yang–Mills energy density. Because it is local, its accuracy increases with the volume on which it is estimated (stochastic locality). Inserting the heat kernel,

$$
\begin{aligned}
\mathcal{E}(t) &= \sum_{x}\sum_{u,v} \big\langle \overline{\psi}(v)\mathcal{K}_{t}^{\dagger}(v \rightarrow x)\overleftrightarrow{\not{\mathcal{D}}}_{x} \mathcal{K}_{t}(u\rightarrow x)\psi(u)\big\rangle \\
&= \sum_{u,v}\sum_{x}\mathcal{K}_{t}^{\dagger}(v \rightarrow x)\overleftrightarrow{\not{\mathcal{D}}}_{x}\mathcal{K}_{t}(u\rightarrow x) \langle \overline{\psi}(v)\psi(u) \rangle \\
&= -\mathrm{tr}\sum_{u,v}\sum_{x}\mathcal{K}_{t}^{\dagger}(v \rightarrow x)\overleftrightarrow{\not{\mathcal{D}}}_{x}\mathcal{K}_{t}(u\rightarrow x)\mathcal{G}(v \rightarrow u)
\end{aligned}
$$

with

$$
\big(\not{\mathcal{D}}+m\big)\mathcal{G}(x \rightarrow y) = \delta_{xy}
$$

the Green's function of the Dirac equation. Now this is where the stochastic estimator will come in. Consider a collection of stochastic source vectors $\eta^{i}$ with the property

$$
\mathbb{E}\big[\eta^{i}(u)\,\eta^{i\dagger}(v)\big] \equiv \lim_{N_{\mathrm{r}}\rightarrow\infty} \frac{1}{N_{\mathrm{r}}}\sum_{i=1}^{N_{\mathrm{r}}}\eta^{i}(u)\eta^{i\dagger}(v) = \delta_{uv}\mathbb{I};
$$

then we can estimate the fermion propagator $\mathcal{G}(x \rightarrow y)$ as

$$
\mathcal{G}(x \rightarrow y) = \mathbb{E}\big[ \mathcal{G}^{i}(y) \eta^{i\dagger}(x) \big],
$$

where

$$
\big(\not{\mathcal{D}}+m\big)\mathcal{G}^{i} \equiv \eta^{i},
$$

to which

$$
\begin{aligned}
\mathcal{E}(t) &= -\mathrm{tr}\mathbb{E}\bigg[\sum_{u,v}\sum_{x}\mathcal{K}_{t}^{\dagger}(v \rightarrow x)\overleftrightarrow{\not{\mathcal{D}}}_{x}\mathcal{K}_{t}(u\rightarrow x)\mathcal{G}^{i}(u)\eta^{i\dagger}(v)\bigg] \\
&= \mathrm{tr}\mathbb{E}\bigg[\sum_{u,v}\sum_{x}\mathcal{K}_{t}(u\rightarrow x)\mathcal{G}^{i}(u)\overleftrightarrow{\not{\mathcal{D}}}_{x}\mathcal{K}_{t}^{\dagger}(v \rightarrow x)\eta^{i\dagger}(v)\bigg] \\
&= \mathbb{E}\sum_{x} \eta_{t}^{i\dagger}(x)\overleftrightarrow{\not{\mathcal{D}}}\mathcal{G}_{t}^{i}(x),
\end{aligned}
$$

leaving us with our final equation:

$$
\mathcal{E}(t) = \mathbb{E}\sum_{x} \eta_{t}^{i\dagger}(x)\overleftrightarrow{\not{\mathcal{D}}}\mathcal{G}_{t}^{i}(x).
$$
