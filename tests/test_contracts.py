"""Unit tests for the data contract layer — parsing, compiling, loading.

No routes and no HTTP here; enforcement over real requests is covered in
tests/test_contract_enforcement.py. These are the tests that have to be right
for the rest to mean anything: if `compile_predicate` can be made to execute
code, or `deny` can be made not to win, nothing downstream matters.
"""

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from gateway.contracts.engine import (
    CompiledContract,
    ContractError,
    QueryNotPermitted,
    compile_contract,
)
from gateway.contracts.expr import PredicateError, compile_predicate
from gateway.contracts.loader import load_registry
from gateway.contracts.spec import ALL_COLUMNS, ContractSpec, Rule

REPO_ROOT = Path(__file__).resolve().parent.parent
CONTRACTS_DIR = REPO_ROOT / "contracts"


def make_spec(**overrides) -> ContractSpec:
    document = {
        "contract": "test-contract",
        "version": "1.0.0",
        "title": "Test",
        "purpose": "Testing.",
        "rules": [{"allow": ["BodyPartExamined"]}],
    }
    document.update(overrides)
    return ContractSpec.model_validate(document)


def compile_spec(**overrides) -> CompiledContract:
    return compile_contract(make_spec(**overrides), "sha256:test")


# --------------------------------------------------------------------------
# The predicate language
# --------------------------------------------------------------------------


class TestPredicateGrammar:
    @pytest.mark.parametrize(
        "source,row,expected",
        [
            ('BodyPartExamined == "FETAL"', {"BodyPartExamined": "FETAL"}, True),
            ('BodyPartExamined == "FETAL"', {"BodyPartExamined": "HEART"}, False),
            ('BodyPartExamined != "FETAL"', {"BodyPartExamined": "HEART"}, True),
            ('BodyPartExamined in ["FETAL", "HEART"]', {"BodyPartExamined": "HEART"}, True),
            ('BodyPartExamined in ["FETAL"]', {"BodyPartExamined": "BRAIN"}, False),
            ('BodyPartExamined not in ["FETAL"]', {"BodyPartExamined": "BRAIN"}, True),
            ('StudyDate >= "20260101"', {"StudyDate": "20260215"}, True),
            ('StudyDate >= "20260101"', {"StudyDate": "20251231"}, False),
            ('not BodyPartExamined == "FETAL"', {"BodyPartExamined": "HEART"}, True),
        ],
    )
    def test_basic_operators(self, source, row, expected):
        assert compile_predicate(source)(row) is expected

    def test_and_or_compose(self):
        predicate = compile_predicate(
            'PatientSex == "F" and BodyPartExamined in ["FETAL", "HEART"]'
        )
        assert predicate({"PatientSex": "F", "BodyPartExamined": "HEART"}) is True
        assert predicate({"PatientSex": "M", "BodyPartExamined": "HEART"}) is False

    def test_chained_comparison(self):
        predicate = compile_predicate('"20260101" <= StudyDate <= "20261231"')
        assert predicate({"StudyDate": "20260615"}) is True
        assert predicate({"StudyDate": "20270101"}) is False

    def test_reports_the_columns_it_reads(self):
        predicate = compile_predicate('PatientSex == "F" and StudyDate > "20260101"')
        assert predicate.columns == frozenset({"PatientSex", "StudyDate"})


class TestPredicateSafety:
    """A contract document must not be a code-execution surface."""

    @pytest.mark.parametrize(
        "source",
        [
            '__import__("os").system("id")',
            "open('/etc/passwd').read()",
            "PatientName.lower()",
            "PatientName.__class__",
            "1 + 1 == 2",
            "[x for x in PatientName]",
            'f"{PatientName}"',
            "lambda: 1",
            "PatientAge if PatientSex else PatientName",
        ],
    )
    def test_constructs_outside_the_grammar_are_refused(self, source):
        with pytest.raises(PredicateError):
            compile_predicate(source)

    def test_the_refusal_names_the_construct(self):
        with pytest.raises(PredicateError, match="Call is not allowed"):
            compile_predicate('__import__("os")')

    def test_syntax_errors_are_reported_not_raised_as_syntaxerror(self):
        with pytest.raises(PredicateError, match="not a valid expression"):
            compile_predicate('BodyPartExamined == = "FETAL"')

    def test_empty_predicate_is_refused(self):
        with pytest.raises(PredicateError):
            compile_predicate("   ")


class TestPredicateMissingColumns:
    """A record lacking the column must never satisfy the predicate.

    This is what makes it safe for a contract to reference GenericCategory
    before the labeling pipeline has ever run.
    """

    def test_missing_column_fails_equality(self):
        assert compile_predicate('GenericCategory == "Neuro"')({}) is False

    def test_missing_column_also_fails_inequality(self):
        # The dangerous case: `!=` on a missing column would otherwise be True
        # and would release the column on every unlabeled record.
        assert compile_predicate('GenericCategory != "Neuro"')({}) is False

    def test_missing_column_fails_membership(self):
        assert compile_predicate('GenericCategory in ["Neuro"]')({}) is False

    def test_mismatched_types_do_not_raise(self):
        assert compile_predicate("PatientAge > 5")({"PatientAge": "031Y"}) is False


# --------------------------------------------------------------------------
# The document schema
# --------------------------------------------------------------------------


class TestRuleValidation:
    def test_a_rule_must_pick_one_of_allow_or_deny(self):
        with pytest.raises(ValidationError, match="exactly one"):
            Rule.model_validate({"allow": ["Diagnosis"], "deny": ["PatientName"]})
        with pytest.raises(ValidationError, match="exactly one"):
            Rule.model_validate({"reason": "nothing"})

    def test_a_bare_string_is_the_same_as_a_one_item_list(self):
        assert Rule.model_validate({"allow": "Diagnosis"}).allow == ["Diagnosis"]

    def test_when_cannot_narrow_a_deny(self):
        with pytest.raises(ValidationError, match="cannot be used with"):
            Rule.model_validate({"deny": ["Diagnosis"], "when": 'PatientSex == "F"'})

    def test_unimplemented_transforms_are_refused_not_ignored(self):
        with pytest.raises(ValidationError, match="not implemented"):
            Rule.model_validate({"allow": ["PatientAge"], "transform": {"bin": 10}})

    def test_unknown_columns_are_caught_as_typos(self):
        with pytest.raises(ValidationError, match="unknown column 'PatientNmae'"):
            Rule.model_validate({"allow": ["PatientNmae"]})

    def test_labeled_columns_are_nameable_before_they_exist(self):
        assert Rule.model_validate({"allow": ["GenericCategory"]}).allow


class TestContractSpecValidation:
    def test_contract_id_must_be_kebab_case(self):
        with pytest.raises(ValidationError, match="kebab-case"):
            make_spec(contract="Fetal_Cardiac")

    def test_version_must_be_semver(self):
        with pytest.raises(ValidationError, match="semver"):
            make_spec(version="1.2")

    def test_unknown_top_level_keys_are_refused(self):
        with pytest.raises(ValidationError):
            make_spec(scope="everything")

    def test_a_contract_needs_at_least_one_rule(self):
        with pytest.raises(ValidationError):
            make_spec(rules=[])

    def test_ref_is_id_at_version(self):
        assert make_spec().ref == "test-contract@1.0.0"


# --------------------------------------------------------------------------
# Compiling a document into a policy
# --------------------------------------------------------------------------


class TestColumnResolution:
    def test_unnamed_columns_are_not_released(self):
        contract = compile_spec(rules=[{"allow": ["BodyPartExamined"]}])
        assert contract.max_columns == frozenset({"BodyPartExamined"})
        assert not contract.releases("PatientName")

    @pytest.mark.parametrize("deny_first", [True, False])
    def test_deny_always_beats_allow_in_either_order(self, deny_first):
        allow = {"allow": ["Diagnosis"]}
        deny = {"deny": ["Diagnosis"]}
        rules = [deny, allow] if deny_first else [allow, deny]
        assert not compile_spec(rules=rules).releases("Diagnosis")

    def test_conditional_release_is_separated_from_unconditional(self):
        contract = compile_spec(
            rules=[
                {"allow": ["BodyPartExamined"]},
                {"allow": ["Diagnosis"], "when": 'BodyPartExamined == "FETAL"'},
            ]
        )
        assert contract.unconditional_columns == frozenset({"BodyPartExamined"})
        assert contract.conditional_columns == frozenset({"Diagnosis"})
        # `releases` means "on every row", which a conditional column is not.
        assert not contract.releases("Diagnosis")

    def test_an_unconditional_allow_is_not_narrowed_by_a_conditional_one(self):
        contract = compile_spec(
            rules=[
                {"allow": ["Diagnosis"]},
                {"allow": ["Diagnosis"], "when": 'BodyPartExamined == "FETAL"'},
            ]
        )
        assert contract.releases("Diagnosis")

    def test_predicates_referencing_unknown_columns_are_refused(self):
        with pytest.raises(ContractError, match="unknown column"):
            compile_spec(
                rules=[{"allow": ["Diagnosis"], "when": 'Nonexistent == "x"'}]
            )

    def test_columns_no_node_serves_are_recorded_not_fatal(self):
        contract = compile_contract(
            make_spec(rules=[{"allow": ["GenericCategory"]}]),
            "sha256:test",
            available_columns=ALL_COLUMNS - {"GenericCategory", "FindingTags"},
        )
        assert contract.unavailable == frozenset({"GenericCategory"})


class TestDerivedColumns:
    def test_reversible_derivation_cannot_outlive_a_denied_source(self):
        # FederatedID is "BCH:BR-7214" — releasing it hands over StudyID.
        with pytest.raises(ContractError, match="contains SourceNode, StudyID verbatim"):
            compile_spec(
                rules=[{"deny": ["StudyID"]}, {"allow": ["FederatedID"]}]
            )

    def test_reversible_derivation_is_fine_when_sources_are_released(self):
        contract = compile_spec(
            rules=[{"allow": ["FederatedID", "SourceNode", "StudyID"]}]
        )
        assert contract.releases("FederatedID")

    def test_declassifying_derivation_may_replace_its_source(self):
        # This is the whole point of the labeling pipeline: GenericCategory is
        # released precisely *because* Diagnosis is not.
        contract = compile_spec(
            rules=[{"deny": ["Diagnosis"]}, {"allow": ["GenericCategory"]}]
        )
        assert contract.releases("GenericCategory")
        assert not contract.releases("Diagnosis")


class TestRowScopeAndProjection:
    @pytest.fixture
    def contract(self):
        return compile_spec(
            row_scope='BodyPartExamined in ["FETAL", "HEART"]',
            rules=[
                {"deny": ["PatientName"]},
                {"allow": ["BodyPartExamined", "PatientSex"]},
                {"allow": ["Diagnosis"], "when": 'BodyPartExamined == "FETAL"'},
            ],
        )

    @pytest.fixture
    def rows(self):
        return [
            {"BodyPartExamined": "FETAL", "PatientSex": "F",
             "PatientName": "A^B", "Diagnosis": "fetal report"},
            {"BodyPartExamined": "HEART", "PatientSex": "M",
             "PatientName": "C^D", "Diagnosis": "cardiac report"},
            {"BodyPartExamined": "BRAIN", "PatientSex": "F",
             "PatientName": "E^F", "Diagnosis": "brain report"},
        ]

    def test_row_scope_removes_rows_entirely(self, contract, rows):
        scoped = contract.scope_rows(rows)
        assert [r["BodyPartExamined"] for r in scoped] == ["FETAL", "HEART"]

    def test_projection_drops_denied_and_unnamed_columns(self, contract, rows):
        projected = contract.project(contract.scope_rows(rows))
        assert all("PatientName" not in row for row in projected)

    def test_conditional_column_appears_only_where_the_predicate_holds(self, contract, rows):
        projected = contract.project(contract.scope_rows(rows))
        fetal, cardiac = projected
        assert fetal["Diagnosis"] == "fetal report"
        assert "Diagnosis" not in cardiac

    def test_projection_omits_columns_the_record_lacks(self, contract):
        projected = contract.project([{"BodyPartExamined": "FETAL"}])
        assert projected == [{"BodyPartExamined": "FETAL"}]


class TestQueryValidation:
    """The redaction-bypass oracle: filtering on a column you cannot read."""

    @pytest.fixture
    def contract(self):
        return compile_spec(
            rules=[
                {"deny": ["PatientName", "PatientID", "StudyID"]},
                {"allow": ["BodyPartExamined", "StudyDate"]},
                {"allow": ["Diagnosis"], "when": 'BodyPartExamined == "FETAL"'},
            ]
        )

    def test_filtering_on_a_released_column_is_fine(self, contract):
        contract.validate_query(body_part=["FETAL"], study_date_from="20260101")

    def test_filtering_on_a_denied_column_is_refused(self, contract):
        with pytest.raises(QueryNotPermitted) as excinfo:
            contract.validate_query(sex="F")
        assert excinfo.value.column == "PatientSex"

    def test_sorting_on_a_denied_column_is_refused(self, contract):
        with pytest.raises(QueryNotPermitted, match="does not release 'PatientAge'"):
            contract.validate_query(sort_by="PatientAge")

    def test_free_text_search_is_refused_when_no_text_column_is_released(self, contract):
        # Diagnosis is conditional; PatientName/StudyID/PatientID are denied.
        with pytest.raises(QueryNotPermitted, match="releases none of the free-text"):
            contract.validate_query(q="Harrington")

    def test_conditionally_released_columns_cannot_be_filtered_on(self):
        contract = compile_spec(
            rules=[
                {"allow": ["BodyPartExamined"]},
                {"allow": ["PatientSex"], "when": 'BodyPartExamined == "FETAL"'},
            ]
        )
        with pytest.raises(QueryNotPermitted, match="only on rows matching"):
            contract.validate_query(sex="F")

    def test_refusal_explains_why_using_the_contract_reason(self):
        contract = compile_spec(
            rules=[
                {"deny": ["PatientSex"], "reason": "not needed for this analysis"},
                {"allow": ["BodyPartExamined"]},
            ]
        )
        with pytest.raises(QueryNotPermitted, match="not needed for this analysis"):
            contract.validate_query(sex="F")

    def test_free_text_search_allowed_when_a_text_column_survives(self):
        contract = compile_spec(rules=[{"allow": ["Diagnosis"]}])
        contract.validate_query(q="infarct")
        assert contract.searchable_columns() == ("Diagnosis",)


class TestDefaultSort:
    def test_prefers_study_date(self):
        assert compile_spec(rules=[{"allow": ["StudyDate", "FederatedID",
                                              "SourceNode", "StudyID"]}]).default_sort() == "StudyDate"

    def test_falls_back_rather_than_failing_a_caller_who_never_sorted(self):
        # A contract denying StudyDate must not 400 every unsorted request.
        contract = compile_spec(rules=[{"allow": ["StudyID"]}])
        assert contract.default_sort() == "StudyID"

    def test_returns_none_when_nothing_sortable_is_released(self):
        assert compile_spec(rules=[{"allow": ["Diagnosis"]}]).default_sort() is None


# --------------------------------------------------------------------------
# Loading contracts/ off disk
# --------------------------------------------------------------------------


def write_contract(directory: Path, name: str, document: dict) -> Path:
    path = directory / f"{name}.yaml"
    path.write_text(yaml.safe_dump(document, sort_keys=False))
    return path


def digest_of(path: Path) -> str:
    import hashlib

    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def contract_dir(tmp_path: Path) -> Path:
    (tmp_path / "acceptance").mkdir()
    write_contract(
        tmp_path,
        "demo-contract",
        {
            "contract": "demo-contract",
            "version": "1.0.0",
            "title": "Demo",
            "purpose": "Testing.",
            "rules": [{"allow": ["BodyPartExamined", "StudyDate"]}],
        },
    )
    (tmp_path / "grants.yaml").write_text(
        yaml.safe_dump({"grants": {"researcher": ["demo-contract@1.0.0"]}})
    )
    return tmp_path


def accept(directory: Path, node: str, ref: str, digest: str) -> None:
    contract, version = ref.split("@")
    (directory / "acceptance" / f"{node.lower()}.yaml").write_text(
        yaml.safe_dump(
            {
                "node": node,
                "accepted": [
                    {"contract": contract, "version": version, "digest": digest}
                ],
            }
        )
    )


class TestLoader:
    def test_loads_and_compiles(self, contract_dir):
        registry = load_registry(contract_dir)
        assert registry.refs() == ["demo-contract@1.0.0"]
        assert registry.is_granted("researcher", "demo-contract@1.0.0")
        assert not registry.is_granted("clinician", "demo-contract@1.0.0")

    def test_acceptance_attaches_nodes(self, contract_dir):
        path = contract_dir / "demo-contract.yaml"
        accept(contract_dir, "BCH", "demo-contract@1.0.0", digest_of(path))
        registry = load_registry(contract_dir)
        assert registry.get("demo-contract@1.0.0").accepted_nodes == frozenset({"BCH"})

    def test_editing_a_contract_revokes_acceptance(self, contract_dir):
        path = contract_dir / "demo-contract.yaml"
        accept(contract_dir, "BCH", "demo-contract@1.0.0", digest_of(path))
        # A committee approved these exact bytes. Change them and the approval
        # refers to a document that no longer exists.
        path.write_text(path.read_text() + "\n# an innocuous-looking edit\n")

        registry = load_registry(contract_dir)
        assert registry.get("demo-contract@1.0.0").accepted_nodes == frozenset()
        assert any("digest" in p or "changed after" in p for p in registry.problems)

    def test_a_hospital_with_no_acceptance_file_has_accepted_nothing(self, contract_dir):
        registry = load_registry(contract_dir)
        assert registry.get("demo-contract@1.0.0").accepted_nodes == frozenset()

    def test_one_bad_contract_does_not_take_down_the_registry(self, contract_dir):
        write_contract(contract_dir, "broken", {"contract": "broken", "version": "oops"})
        registry = load_registry(contract_dir)
        assert registry.refs() == ["demo-contract@1.0.0"]
        assert any("broken.yaml" in problem for problem in registry.problems)

    def test_filename_must_match_the_contract_id(self, contract_dir):
        write_contract(
            contract_dir,
            "misnamed",
            {
                "contract": "something-else",
                "version": "1.0.0",
                "title": "T",
                "purpose": "P",
                "rules": [{"allow": ["StudyDate"]}],
            },
        )
        registry = load_registry(contract_dir)
        assert registry.refs() == ["demo-contract@1.0.0"]

    def test_grants_naming_a_missing_contract_are_flagged(self, contract_dir):
        (contract_dir / "grants.yaml").write_text(
            yaml.safe_dump({"grants": {"researcher": ["ghost-contract@9.9.9"]}})
        )
        registry = load_registry(contract_dir)
        assert any("ghost-contract@9.9.9" in problem for problem in registry.problems)

    def test_missing_directory_is_survivable(self, tmp_path):
        registry = load_registry(tmp_path / "nope")
        assert registry.refs() == []
        assert registry.problems


@pytest.fixture(scope="module")
def registry():
    return load_registry(CONTRACTS_DIR)


class TestShippedContracts:
    """The contracts in contracts/ must actually be valid and self-consistent."""

    def test_every_shipped_contract_loads_without_problems(self, registry):
        assert registry.problems == []

    def test_all_three_are_present(self, registry):
        assert registry.refs() == [
            "clinical-full-access@1.0.0",
            "fetal-cardiac-outcomes@1.2.0",
            "population-health@1.0.0",
        ]

    def test_full_access_is_accepted_everywhere(self, registry):
        contract = registry.get("clinical-full-access@1.0.0")
        assert contract.accepted_nodes == frozenset({"BCH", "MGH", "BWH"})

    def test_acceptance_is_deliberately_uneven(self, registry):
        # Partial federation should be demonstrable as a governance outcome,
        # not only as an outage.
        assert registry.get("fetal-cardiac-outcomes@1.2.0").accepted_nodes == frozenset(
            {"BCH", "MGH"}
        )
        assert registry.get("population-health@1.0.0").accepted_nodes == frozenset(
            {"BCH", "BWH"}
        )

    def test_population_health_releases_no_free_text(self, registry):
        contract = registry.get("population-health@1.0.0")
        assert not contract.releases("Diagnosis")
        assert contract.releases("GenericCategory")

    def test_no_shipped_contract_releases_a_direct_identifier_by_accident(self, registry):
        identifiers = {"PatientName", "PatientID", "PatientBirthDate", "StudyInstanceUID"}
        for ref, contract in registry.contracts.items():
            if ref.startswith("clinical-full-access"):
                continue  # treatment purpose, identifiers are the point
            assert not (contract.max_columns & identifiers), ref
