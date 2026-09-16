"""Descomposicion Nested Benders por años para el modelo de battery swapping.

Portada desde la rama carga_ob_multiaño. La division de responsabilidades es la
misma: `year_block.py` es lo unico especifico del modelo (arma el subproblema de
un año y publica su vector de estado en `state_links`), mientras que `cuts.py`,
`passes.py` y `driver.py` operan solo sobre ese registro y no saben nada de
naves, baterias ni degradacion.
"""
