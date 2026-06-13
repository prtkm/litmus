"""Vision over figures (DESIGN §5 frontier, §19 WS-E).

The novelty frontier of LITMUS is *figures + T2*: a vision reasoner reads plotted values off a
figure so a deterministic verifier can check them against the table (DESIGN §5: "Most of these
collapse to 'read numbers off the figure -> T0/T2 deterministic check'"). This package holds the
*reading* half — :func:`litmus.vision.figure_reader.read_figure_values`, which sends a figure image
to Opus 4.8 vision and returns the extracted numbers. The *judging* half lives in the deterministic
verifiers (``litmus.verifiers.figure_vs_table``), keeping the model out of the verdict (DESIGN §3.1).
"""

from litmus.vision.figure_reader import FigureReadError, read_figure_values

__all__ = ["read_figure_values", "FigureReadError"]
