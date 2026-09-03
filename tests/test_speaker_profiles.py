from workcall_transcriber.speaker_profiles import SpeakerProfile, match_embedding, updated_centroid


def test_profile_match_requires_the_configured_conservative_threshold() -> None:
    profile = SpeakerProfile("p1", "Mikhail", (1.0, 0.0), 1)

    match = match_embedding((0.99, 0.01), [profile], threshold=0.95)
    rejected = match_embedding((0.7, 0.7), [profile], threshold=0.95)

    assert match is not None
    assert match.display_name == "Mikhail"
    assert match.similarity > 0.95
    assert rejected is None


def test_centroid_update_keeps_reference_embeddings_normalized() -> None:
    centroid = updated_centroid((1.0, 0.0), 1, (0.0, 1.0))

    assert round(sum(value * value for value in centroid), 6) == 1.0
