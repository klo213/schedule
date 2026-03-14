import pandas as pd

def validate_row(row):
    """
    Validates a single row (as a pandas Series) of the schedule data.
    """
    required_columns = ["event_type", "team", "resource", "start_datetime", "end_datetime"]
    errors = []

    for col in required_columns:
        if col not in row:
            errors.append(f"Missing required column: {col}")
        elif pd.isna(row[col]) or str(row[col]).strip() == "":
            errors.append(f"Column '{col}' cannot be empty")

    if errors:
        return False, errors
    
    return True, None
