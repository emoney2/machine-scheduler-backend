import os

def get_data_provider():
    """
    Decides where data comes from.
    """
    if os.getenv("USE_SUPABASE") == "1":
        return "supabase"
    return "sheets"
