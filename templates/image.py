import matplotlib.pyplot as plt

# Define project tasks and their durations (start and end week)
tasks = [
    ("Decide the Goal", 1, 3),
    ("Name and Approach", 2, 5),
    ("Set Up Project Environment", 3, 6),
    ("Making of Project", 6, 10),
    ("Testing of the Project", 9, 12),
    ("Making the Required Changes", 11, 14),
    ("Deployment of Project", 13, 16),
]

task_labels = [task[0] for task in tasks]

# Setup weeks and months
weeks = list(range(1, 17))
months = ["JULY", "AUGUST", "SEPTEMBER", "OCTOBER"]
month_boundaries = [1, 5, 9, 13, 17]  # Start week of each month

# Plot setup
fig, ax = plt.subplots(figsize=(12, 6))
ax.set_xlim(0, 16)
ax.set_ylim(0, len(tasks))
ax.set_xticks(range(0, 16))
ax.set_xticklabels([str(i + 1) for i in range(16)])
ax.set_yticks(range(len(tasks)))
ax.set_yticklabels(task_labels)

# Draw bars for tasks
for i, (task, start, end) in enumerate(tasks):
    ax.barh(i, end - start + 1, left=start - 1, height=0.5, color="goldenrod")

# Month labels
for i in range(len(months)):
    x_center = (month_boundaries[i] + month_boundaries[i+1] - 2) / 2
    ax.text(x_center, len(tasks) + 0.2, months[i], ha='center', va='bottom', fontsize=10, fontweight='bold')

# Aesthetic adjustments
ax.grid(True, axis='x', linestyle='--', alpha=0.6)
ax.invert_yaxis()
ax.set_title("Project Planning Chart (July – October)", fontsize=14, fontweight='bold')

# Clean up frame
for spine in ['top', 'right', 'left']:
    ax.spines[spine].set_visible(False)

plt.tight_layout()

# Save as image
plt.savefig("project_planning_chart.png", dpi=300)
plt.show()
