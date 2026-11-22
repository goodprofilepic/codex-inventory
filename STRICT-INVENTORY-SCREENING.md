# Strict Reading Room batch workflow

This branch contains a separate batch builder that loads the latest Reading Room inventory from `main`, normalizes author aliases and title variants, and rejects existing or within-batch duplicates. It does not change the live inventory or publish anything to Scriptorium.
