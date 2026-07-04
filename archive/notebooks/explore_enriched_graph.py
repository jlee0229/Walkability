from walkability.graph.build import inspect_edges

# 5 random edges across all sources (default)
inspect_edges()

# Check that city-matched footways have real SCI scores
inspect_edges(n=10, source="city_inventory", highway="footway")

# Verify geometric fallback edges look reasonable
inspect_edges(n=4, source="geometric")

# Spot-check context-inferred edges
inspect_edges(n=5, source="context")

# Reproducible sample — change seed to get a different draw
inspect_edges(n=5, seed=99)