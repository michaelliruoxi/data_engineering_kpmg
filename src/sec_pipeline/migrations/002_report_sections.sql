-- Section identity belongs to the exact cleaned report, never just a filing.
CREATE TABLE sec.section_types (
    code text PRIMARY KEY CHECK (btrim(code) <> ''),
    label text NOT NULL CHECK (btrim(label) <> ''),
    topic text NOT NULL CHECK (btrim(topic) <> '')
);

INSERT INTO sec.section_types (code, label, topic) VALUES
    ('unknown', 'Unclassified content', 'other'),
    ('front_matter', 'Front matter', 'overview'),
    ('part_heading', 'Part heading', 'other'),
    ('business', 'Business', 'overview'),
    ('risk_factors', 'Risk factors', 'risks'),
    ('cybersecurity', 'Cybersecurity', 'risks'),
    ('properties', 'Properties', 'overview'),
    ('legal_proceedings', 'Legal proceedings', 'risks'),
    ('mine_safety', 'Mine safety disclosures', 'compliance'),
    ('market_equity', 'Market for common equity', 'financials'),
    ('equity_sales', 'Unregistered sales of equity securities', 'financials'),
    ('defaults', 'Defaults upon senior securities', 'risks'),
    ('other_information', 'Other information', 'other'),
    ('exhibits', 'Exhibits', 'other'),
    ('financial_statements', 'Financial statements', 'financials'),
    ('mda', 'Management discussion and analysis', 'management'),
    ('market_risk', 'Market risk disclosures', 'risks'),
    ('controls', 'Controls and procedures', 'governance'),
    ('accounting_changes', 'Accounting changes and disagreements', 'financials'),
    ('directors_governance', 'Directors and corporate governance', 'governance'),
    ('executive_compensation', 'Executive compensation', 'governance'),
    ('ownership', 'Security ownership', 'governance'),
    ('related_party_transactions', 'Related-party transactions and independence', 'governance'),
    ('audit_fees', 'Principal accountant fees and services', 'governance'),
    ('summary', 'Form summary', 'overview');

CREATE TABLE sec.report_sections (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    filing_id uuid NOT NULL REFERENCES sec.filings(id),
    report_id uuid NOT NULL,
    section_key text NOT NULL CHECK (btrim(section_key) <> ''),
    section_type text NOT NULL DEFAULT 'unknown' REFERENCES sec.section_types(code),
    original_title text NOT NULL CHECK (btrim(original_title) <> ''),
    part text CHECK (part IS NULL OR btrim(part) <> ''),
    item text CHECK (item IS NULL OR btrim(item) <> ''),
    section_order integer NOT NULL CHECK (section_order >= 0),
    offset_start integer NOT NULL CHECK (offset_start >= 0),
    offset_end integer NOT NULL CHECK (offset_end > offset_start),
    source_locator jsonb NOT NULL DEFAULT '{}'::jsonb
        CHECK (jsonb_typeof(source_locator) = 'object'),
    validation_status text NOT NULL DEFAULT 'unverified'
        CHECK (validation_status IN ('unverified', 'detected', 'validated')),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (report_id, section_key),
    UNIQUE (report_id, section_order),
    UNIQUE (id, report_id, filing_id),
    FOREIGN KEY (report_id, filing_id) REFERENCES sec.reports(id, filing_id) ON DELETE CASCADE
);

CREATE INDEX report_sections_type_idx ON sec.report_sections(section_type, report_id);
CREATE INDEX report_sections_filing_idx ON sec.report_sections(filing_id);

ALTER TABLE sec.chunks ADD COLUMN section_id uuid;
ALTER TABLE sec.chunks ADD CONSTRAINT chunks_section_same_report_fk
    FOREIGN KEY (section_id, report_id, filing_id)
    REFERENCES sec.report_sections(id, report_id, filing_id);
CREATE INDEX chunks_section_idx ON sec.chunks(section_id) WHERE section_id IS NOT NULL;

CREATE FUNCTION sec.validate_report_section() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    report_text text;
BEGIN
    -- Serialize section writes and report edits against the same parent row.
    SELECT full_text INTO report_text FROM sec.reports
        WHERE id = NEW.report_id AND filing_id = NEW.filing_id FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'Section report does not exist in the specified filing'
            USING ERRCODE = '23503';
    END IF;
    IF NEW.offset_end > char_length(report_text) THEN
        RAISE EXCEPTION 'Section exceeds its report character range' USING ERRCODE = '23514';
    END IF;
    IF EXISTS (SELECT 1 FROM sec.report_sections s
               WHERE s.report_id = NEW.report_id AND s.id <> NEW.id
                 AND s.section_key <> NEW.section_key
                 AND s.offset_start < NEW.offset_end AND s.offset_end > NEW.offset_start) THEN
        RAISE EXCEPTION 'Report section ranges must not overlap' USING ERRCODE = '23514';
    END IF;
    IF TG_OP = 'UPDATE' AND EXISTS (
        SELECT 1 FROM sec.chunks c WHERE c.section_id = OLD.id
          AND (c.report_id <> NEW.report_id OR c.filing_id <> NEW.filing_id
               OR c.offset_start < NEW.offset_start OR c.offset_end > NEW.offset_end)
    ) THEN
        RAISE EXCEPTION 'Section edit would invalidate existing chunks' USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER report_sections_validate_ranges
BEFORE INSERT OR UPDATE ON sec.report_sections
FOR EACH ROW EXECUTE FUNCTION sec.validate_report_section();

CREATE FUNCTION sec.validate_chunk_section() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    section_start integer;
    section_end integer;
BEGIN
    IF NEW.section_id IS NULL THEN RETURN NEW; END IF;
    SELECT offset_start, offset_end INTO section_start, section_end
        FROM sec.report_sections
        WHERE id = NEW.section_id AND report_id = NEW.report_id AND filing_id = NEW.filing_id
        FOR SHARE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'Chunk section belongs to another report or does not exist'
            USING ERRCODE = '23503';
    END IF;
    IF NEW.offset_start < section_start OR NEW.offset_end > section_end THEN
        RAISE EXCEPTION 'Chunk crosses its report section boundary' USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER chunks_validate_section
BEFORE INSERT OR UPDATE OF section_id, report_id, filing_id, offset_start, offset_end
ON sec.chunks FOR EACH ROW EXECUTE FUNCTION sec.validate_chunk_section();

CREATE FUNCTION sec.validate_report_section_content() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.full_text IS DISTINCT FROM OLD.full_text AND EXISTS (
        SELECT 1 FROM sec.report_sections s WHERE s.report_id = OLD.id
          AND (s.offset_end > char_length(NEW.full_text)
               OR substring(NEW.full_text FROM s.offset_start + 1 FOR s.offset_end - s.offset_start)
                  IS DISTINCT FROM
                  substring(OLD.full_text FROM s.offset_start + 1 FOR s.offset_end - s.offset_start))
    ) THEN
        RAISE EXCEPTION 'Report edit would invalidate section content; create a new extraction version'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER reports_validate_section_content
BEFORE UPDATE OF full_text ON sec.reports
FOR EACH ROW EXECUTE FUNCTION sec.validate_report_section_content();

-- Only valid, nonoverlapping legacy descriptors are imported. Labels are not
-- upgraded to verified classifications; malformed metadata remains inspectable
-- in the original reports.sections JSON and its chunks remain unassigned.
DO $$
DECLARE
    r record;
    descriptor jsonb;
    ordinal integer;
    key_text text;
    start_number numeric;
    end_number numeric;
BEGIN
    FOR r IN SELECT id, filing_id, full_text, sections FROM sec.reports LOOP
        ordinal := 0;
        FOR descriptor IN SELECT value FROM jsonb_array_elements(r.sections) LOOP
            key_text := coalesce(descriptor->>'section_key', descriptor->>'section_id',
                                 'legacy-section-' || ordinal::text);
            IF jsonb_typeof(descriptor) = 'object' AND btrim(coalesce(key_text, '')) <> ''
               AND coalesce(descriptor->>'offset_start', '') ~ '^[0-9]{1,10}$'
               AND coalesce(descriptor->>'offset_end', '') ~ '^[0-9]{1,10}$' THEN
                start_number := (descriptor->>'offset_start')::numeric;
                end_number := (descriptor->>'offset_end')::numeric;
                IF start_number < end_number AND end_number <= char_length(r.full_text)
                   AND NOT EXISTS (SELECT 1 FROM sec.report_sections s WHERE s.report_id = r.id
                                   AND (s.section_key = key_text
                                        OR (s.offset_start < end_number AND s.offset_end > start_number))) THEN
                    INSERT INTO sec.report_sections
                        (filing_id, report_id, section_key, original_title, part, item, section_order,
                         offset_start, offset_end, source_locator)
                    VALUES (r.filing_id, r.id, key_text,
                        coalesce(nullif(btrim(descriptor->>'original_title'), ''),
                                 nullif(btrim(descriptor->>'title'), ''), key_text),
                        CASE WHEN jsonb_typeof(descriptor->'part') = 'string'
                             THEN nullif(btrim(descriptor->>'part'), '') END,
                        CASE WHEN jsonb_typeof(descriptor->'item') = 'string'
                             THEN nullif(btrim(descriptor->>'item'), '') END, ordinal,
                        start_number::integer, end_number::integer,
                        CASE WHEN jsonb_typeof(descriptor->'source_locator') = 'object'
                             THEN descriptor->'source_locator'
                             WHEN jsonb_typeof(descriptor->'source_locator') = 'string'
                             THEN jsonb_build_object('legacy_locator', descriptor->>'source_locator')
                             ELSE '{}'::jsonb END);
                END IF;
            END IF;
            ordinal := ordinal + 1;
        END LOOP;
    END LOOP;
END;
$$;

UPDATE sec.chunks c SET section_id = s.id
FROM sec.report_sections s
WHERE c.report_id = s.report_id AND c.filing_id = s.filing_id
  AND coalesce(c.section, c.metadata->>'section_id') = s.section_key
  AND c.offset_start >= s.offset_start AND c.offset_end <= s.offset_end;

COMMENT ON TABLE sec.report_sections IS
    'Queryable sections of one exact report extraction; reports.sections preserves extractor input.';
COMMENT ON COLUMN sec.report_sections.validation_status IS
    'unverified: legacy or unchecked; detected: rule-based extraction; validated: explicitly reviewed.';
COMMENT ON COLUMN sec.report_sections.offset_start IS
    'Inclusive zero-based Unicode character offset into the referenced report full_text.';
COMMENT ON COLUMN sec.report_sections.offset_end IS
    'Exclusive zero-based Unicode character offset into the referenced report full_text.';
