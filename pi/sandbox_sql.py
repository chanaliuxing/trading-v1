import traceback
from libs.db.ch_pool import get_client

def dry_run_feature_sql(sql_expression: str, table_alias: str = 'm5') -> dict:
    """
    For Brain to use as a dry run tool.
    :return: {"ok": bool, "error": str, "sample_value": any}
    """
    client = get_client()
    
    # 1. Construct a very lightweight test query (LIMIT 1)
    # Note: This may need adjustment based on your actual table names, e.g., trading.features_5m
    test_query = f"""
    SELECT 
        {sql_expression} as test_feature 
    FROM trading.features_5m AS {table_alias}
    JOIN trading.features_1m AS f ON toStartOfInterval(f.ts, INTERVAL 5 minute) = {table_alias}.ts
    LIMIT 1
    """
    
    try:
        # 2. Try to execute
        result = client.execute(test_query)
        
        # 3. Success: Return success signal and a sample value (for AI to confirm value range)
        sample = result[0][0] if result and result[0] else 0.0
        return {
            "ok": True,
            "error": None,
            "sample_value": str(sample),
            "message": "Dry run successful. Syntax valid."
        }
        
    except Exception as e:
        # 4. Failure: Capture the specific ClickHouse error
        error_msg = str(e).split('\n')[0] # Take only the first line of the error to save tokens
        return {
            "ok": False,
            "error": error_msg, # Key: Pass back "Unknown identifier" etc.
            "sample_value": None,
            "message": f"SQL Error: {error_msg}"
        }

def filter_valid_derived(recipe: dict, feature_catalog: list) -> tuple[dict, list]:
    """
    Filters derived features in a recipe by performing a dry run for each.

    Args:
        recipe (dict): The feature recipe containing base and derived features.
        feature_catalog (list): List of available columns in the table.

    Returns:
        tuple[dict, list]: A tuple containing the modified recipe with only valid
                           derived features, and a list of dropped features with
                           their corresponding error messages.
    """
    if not recipe.get("derived"):
        return recipe, []

    valid_derived = []
    dropped_derived = []
    
    for derived_feature in recipe["derived"]:
        sql_expr = derived_feature.get("expr")
        alias = derived_feature.get("alias")
        if not sql_expr or not alias:
            continue

        # Perform the dry run
        result = dry_run_feature_sql(sql_expr)

        if result["ok"]:
            valid_derived.append(derived_feature)
        else:
            dropped_derived.append({
                "alias": alias,
                "expr": sql_expr,
                "error": result["error"]
            })
            print(f"[Sandbox] Dropping derived feature '{alias}': {result['error']}")

    # Create a new recipe with only the valid derived features
    new_recipe = recipe.copy()
    new_recipe["derived"] = valid_derived
    
    return new_recipe, dropped_derived
        