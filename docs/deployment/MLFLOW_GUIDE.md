# MLflow Guide: How to Track Your AI Trader Experiments

This guide explains what MLflow is and how to use its web interface to find and understand the results from the `learn` command.

## 1. What is MLflow?

MLflow is a platform for managing the entire machine learning lifecycle. In this project, we primarily use it for **experiment tracking**.

- **Experiment**: A collection of related runs, like a folder. For example, all the HPO runs for a specific strategy could be one experiment.
- **Run**: A single execution of your model training code. When you run the `learn` command, it performs HPO, which involves dozens or hundreds of trials. Each of these trials is logged as a **run** in MLflow.

For each run, MLflow records:
- **Parameters**: The hyperparameters used for that specific trial (e.g., `max_depth`, `n_estimators`).
- **Metrics**: The performance results of the model (e.g., `sharpe`, `total_return`, `oos_avg_ic`).
- **Artifacts**: Any output files, such as plots, logs, or the saved model file itself.

The web UI at `http://localhost:5001` is your window into all of this data.

## 2. How to Find Your Currently Running Experiment

When you execute the `learn` command, a new HPO process starts. Here’s how to find it in the MLflow UI:

1.  **Open the MLflow UI**: Navigate to `http://localhost:5001` in your browser.

2.  **Select the Experiment**:
    - On the left-hand sidebar, you will see a list of experiments.
    - The HPO runs from `alpha_loop.py` are automatically logged to an experiment, often named after the strategy or configuration (e.g., `alpha_optuna_...`).
    - **Click on the most recent experiment** at the top of the list. If you're unsure, the default is often just called "Default".

    ![MLflow Experiment List](https://i.imgur.com/s03t43d.png)

3.  **Find Your Run**:
    - After clicking an experiment, the main panel will show a table with all the runs for that experiment.
    - The run at the very top, with the most recent "Start Time", is the one that was just started by your `learn` command.
    - The `learn` command actually executes many trials, so you will see new runs appearing at the top of this list as the HPO progresses.

    ![MLflow Runs Table](https://i.imgur.com/hZ3bJ8K.png)

4.  **View Run Details**:
    - Click on the name of the top run (e.g., "serene-fowl-245") to see its details.

## 3. Understanding the Run Details Page

Once you click on a specific run, you'll see all the information logged for that trial.

- **Parameters**: A simple list of all the hyper-parameters that were used. This tells you the exact configuration for this trial.

- **Metrics**: This section shows the key performance indicators for the model trained in this run.
    - You can see final scores like `oos_avg_sharpe`, `oos_avg_ic`, and `best_value`.
    - You can click on any metric to see a chart of its value over training steps (if applicable).

- **Artifacts**: This is a file browser for all files saved during the run. You might find:
    - **Plots**: `.png` files visualizing performance, such as `proposer_contrib.png`.
    - **Configuration**: `.json` files that store the exact configuration used.
    - **Results**: `.csv` files with detailed row-by-row results (`rows_with_origin.csv`).
    - **Model Files**: Sometimes the trained model itself is saved here (e.g., in a `model` directory).

By comparing the parameters and metrics across different runs in an experiment, you can understand which hyper-parameters lead to the best model performance. The UI allows you to select multiple runs and click "Compare" to see a detailed side-by-side comparison.
