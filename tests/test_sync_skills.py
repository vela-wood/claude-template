import sync_skills


def test_agents_skills_mirror_matches_canonical():
    assert sync_skills.differences() == []
