"""One local match cache shared by dashboard, details and resume generation."""
import hashlib
import json
from threading import RLock

from job_agent.services.local_matcher import match_job_locally, structure_job_locally

# Bump when local parsing/scoring semantics change.
MATCH_CACHE_VERSION = 'local-profile-jd-v1'
_lock = RLock()


def ensure_current_match(repository, profile, job):
    source = job.sources[0] if job.sources else None
    inputs = {
        'version': MATCH_CACHE_VERSION,
        'profile': profile.model_dump(mode='json'),
        'job': [job.company, job.title, job.location, job.jd_text,
                source.platform if source else 'dashboard', source.source_url if source else None],
    }
    fingerprint = hashlib.sha256(json.dumps(inputs, ensure_ascii=False, sort_keys=True).encode('utf-8')).hexdigest()
    with _lock:
        previous = repository.get_latest_match_result(job.job_id)
        if previous is not None and previous.input_fingerprint == fingerprint:
            return previous
        structured = structure_job_locally(job.jd_text, company=job.company,
            title=job.title, location=job.location,
            source=source.platform if source else 'dashboard',
            source_url=source.source_url if source else None)
        result = match_job_locally(profile, structured)
        result.input_fingerprint = fingerprint
        repository.add_match_result(job.job_id, result)
        return result


def refresh_all_matches(repository, profile):
    """No API calls and no application/status writes."""
    profile_data = profile.model_dump(mode='json')
    pending = []
    # Keep the existing per-job cache contract, but score and persist misses in
    # batches. This avoids reopening and migrating SQLite for every new job.
    with _lock:
        for row in repository.match_refresh_inputs():
            source = row['platform'] or 'dashboard'
            inputs = {'version': MATCH_CACHE_VERSION, 'profile': profile_data,
                      'job': [row['company'], row['title'], row['location'], row['jd_text'],
                              source, row['source_url']]}
            fingerprint = hashlib.sha256(json.dumps(inputs, ensure_ascii=False, sort_keys=True).encode('utf-8')).hexdigest()
            try:
                cached = json.loads(row['result_json'] or '{}')
            except (ValueError, TypeError):
                cached = {}
            if isinstance(cached, dict) and cached.get('input_fingerprint') == fingerprint:
                continue
            structured = structure_job_locally(
                row['jd_text'], company=row['company'], title=row['title'],
                location=row['location'], source=source,
                source_url=row['source_url'],
            )
            result = match_job_locally(profile, structured)
            result.input_fingerprint = fingerprint
            pending.append((row['id'], result))
            if len(pending) >= 100:
                repository.add_match_results(pending)
                pending = []
        if pending:
            repository.add_match_results(pending)
