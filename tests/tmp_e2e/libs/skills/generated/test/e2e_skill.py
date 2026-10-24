
def on_decision(decision, context):
    # Test hook: invert decision confidence
    if 'confidence' in decision:
        decision['confidence'] = 0.99
    return decision
