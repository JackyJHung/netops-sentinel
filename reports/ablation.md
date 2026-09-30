| config | precision | recall | f1 | mttd_min | rca_top1 | rca_top3 | classification_acc | alert_compression | n_alerts | n_incidents |
|---|---|---|---|---|---|---|---|---|---|---|
| static thresholds (baseline) | 1.0 | 0.491 | 0.647 | 3.975 | 1.0 | 1.0 | 0.938 | 1.474 | 6.6 | 4.4 |
| robust_z + ewma only | 1.0 | 0.929 | 0.959 | 3.607 | 1.0 | 1.0 | 0.78 | 2.447 | 20.2 | 8.3 |
| + iforest | 1.0 | 0.975 | 0.987 | 4.007 | 1.0 | 1.0 | 0.744 | 2.993 | 26.2 | 8.7 |
| + forecast | 1.0 | 0.988 | 0.993 | 1.903 | 1.0 | 1.0 | 0.969 | 3.213 | 28.3 | 8.8 |
| + log mining (full sentinel) | 0.94 | 1.0 | 0.968 | 1.972 | 1.0 | 1.0 | 0.99 | 4.75 | 45.3 | 9.5 |
