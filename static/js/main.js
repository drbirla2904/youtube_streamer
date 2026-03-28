// Auto-dismiss alerts after 5 seconds
document.addEventListener('DOMContentLoaded', function() {
    const alerts = document.querySelectorAll('.alert:not(.alert-permanent)');
    alerts.forEach(function(alert) {
        setTimeout(function() {
            const bsAlert = new bootstrap.Alert(alert);
            bsAlert.close();
        }, 5000);
    });
});

// Confirm delete actions
document.querySelectorAll('[data-confirm]').forEach(function(element) {
    element.addEventListener('click', function(e) {
        if (!confirm(this.getAttribute('data-confirm'))) {
            e.preventDefault();
        }
    });
});

// File upload preview
const fileInputs = document.querySelectorAll('input[type="file"]');
fileInputs.forEach(function(input) {
    input.addEventListener('change', function(e) {
        const fileName = e.target.files[0]?.name;
        if (fileName) {
            const label = input.nextElementSibling;
            if (label && label.classList.contains('custom-file-label')) {
                label.textContent = fileName;
            }
        }
    });
});
