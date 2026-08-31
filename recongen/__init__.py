"""Synthetic POS -> processor settlement -> bank statement generator.

Produces a reconciliation dataset with ground truth: sales as a restaurant/retail
POS records them, settlements as each processor pays them out, and bank lines as
they actually land - with the timing, fee and aggregation mismatches that make
reconciliation hard in real life.
"""

__version__ = "0.1.0"
