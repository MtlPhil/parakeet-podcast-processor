"""Job wrappers around the P3 pipeline modules.

Each task takes a job_id and records status and progress in the jobs table.
Tasks are executed one at a time by the serial runner in job_queue.py.
Pipeline modules are imported inside each task so the API starts quickly
without loading ML dependencies.

- ``pipeline``: fetching, transcribing, digesting, the full pipeline
- ``content``: exports, blog posts, LinkedIn posts, synopses
- ``ask``: Ask's index and its evaluation
- ``daily_run``: each user's daily run, its report and the backlog
- ``common``: what they share

Import names from the module that defines them (and patch them there in
tests): this package re-exports nothing.
"""
