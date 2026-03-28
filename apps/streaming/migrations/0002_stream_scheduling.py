# Generated migration for stream scheduling support

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('streaming', '0001_initial'),
    ]

    operations = [
        # Add scheduled status to STATUS_CHOICES
        migrations.RunSQL(
            sql="-- This is handled by the model definition change",
            reverse_sql="-- Reverse not needed for choice changes"
        ),
        
        # Add scheduled_start_time field
        migrations.AddField(
            model_name='stream',
            name='scheduled_start_time',
            field=models.DateTimeField(
                null=True,
                blank=True,
                db_index=True,
                help_text='When the stream should automatically start'
            ),
        ),
        
        # Update the check constraint to include 'scheduled' status
        migrations.RemoveConstraint(
            model_name='stream',
            name='valid_stream_status',
        ),
        
        migrations.AddConstraint(
            model_name='stream',
            constraint=models.CheckConstraint(
                check=models.Q(
                    status__in=['idle', 'scheduled', 'starting', 'running', 'stopping', 'stopped', 'error']
                ),
                name='valid_stream_status'
            ),
        ),
        
        # Add index for finding scheduled streams
        migrations.AddIndex(
            model_name='stream',
            index=models.Index(
                fields=['status', 'scheduled_start_time'],
                name='stream_scheduled_idx',
            ),
        ),
    ]
