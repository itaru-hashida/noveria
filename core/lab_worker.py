"""Фоновый процесс эксперимента, независимый от жизненного цикла Streamlit."""
import sys

from core.lab import execute_experiment

if __name__ == '__main__':
    execute_experiment(sys.argv[1])
