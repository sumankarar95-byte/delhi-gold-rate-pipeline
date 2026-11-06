# Foundation-model benchmark

500 daily walk-forward origins · foundation-model context 1024 days · errors are log-return errors · DM = Diebold-Mariano p-value vs naive (lower = more convincing).

## next_day (1 trading days)

| Model | MAE % | vs naive | DM p | Direction | 80% band coverage |
|---|---|---|---|---|---|
| momentum | 1.025 | +0.2% | 0.660 | 54% |  |
| naive | 1.027 | +0.0% |  |  |  |
| ar1 | 1.031 | -0.4% | 0.396 | 52% |  |
| holt_damped | 1.032 | -0.5% | 0.387 | 53% |  |
| timesfm2.5 | 1.053 | -2.6% | 0.014 | 46% | 91% |
| chronos2 | 1.082 | -5.4% | 0.005 | 51% | 88% |

## one_week (5 trading days)

| Model | MAE % | vs naive | DM p | Direction | 80% band coverage |
|---|---|---|---|---|---|
| ar1 | 2.292 | +1.6% | 0.114 | 61% |  |
| holt_damped | 2.309 | +0.8% | 0.618 | 59% |  |
| momentum | 2.310 | +0.8% | 0.604 | 59% |  |
| naive | 2.328 | +0.0% |  |  |  |
| timesfm2.5 | 2.361 | -1.4% | 0.235 | 46% | 81% |
| chronos2 | 2.362 | -1.4% | 0.394 | 48% | 71% |

## one_month (22 trading days)

| Model | MAE % | vs naive | DM p | Direction | 80% band coverage |
|---|---|---|---|---|---|
| ar1 | 4.882 | +5.6% | 0.139 | 70% |  |
| momentum | 4.899 | +5.2% | 0.347 | 67% |  |
| holt_damped | 4.927 | +4.7% | 0.326 | 69% |  |
| chronos2 | 5.013 | +3.0% | 0.394 | 66% | 64% |
| naive | 5.170 | +0.0% |  |  |  |
| timesfm2.5 | 5.196 | -0.5% | 0.722 | 51% | 76% |

Runtime (CPU, Apple M4): chronos2 12s, timesfm2.5 19s
