# Security and data-boundary policy

This public portfolio intentionally contains only CVAT Task #25 evidence, a small set of derived visual samples, selected sample labels, and reproducibility code.

Do not add any of the following:

- CVAT passwords, API tokens, cookies, or `.env` files;
- task backups inside Git history or Docker volume exports;
- private server addresses, SSH configuration, or laboratory filesystem paths;
- images or annotations from other CVAT tasks;
- the complete 200-image working subset.

Before every release, run the repository validator and inspect `git diff --cached`. If sensitive material is accidentally committed, rotate the credential first, then remove it from Git history before publishing again.

The only approved full backup is Task #25's checksum-locked GitHub Release asset. It contains
public MAR20-derived images and annotations, but no credentials, tokens, private server addresses,
other CVAT tasks or database volume. Any replacement asset must pass `recovery/verify_backup.py`
and retain the CC BY-NC 4.0 attribution.
