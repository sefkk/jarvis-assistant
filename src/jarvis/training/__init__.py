"""Training log and Apple Health import.

- models.py        Pydantic input models for log_training and the validated Health record
- store.py         WorkoutStore: all SQL for workouts, workout_sets and health_imports
- summary.py       pure aggregation and plain-text formatting for training_summary
- health_import.py inbox JSON adapters (Shortcut, Health Auto Export v1/v2) and export.zip streaming
"""
