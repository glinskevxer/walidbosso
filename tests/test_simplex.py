from fractions import Fraction as F

import pytest

from binyard.blend.simplex import (
    Constraint,
    LinearProgram,
    Sense,
    Solution,
    Status,
    solve,
)
from binyard.errors import ValidationError


def program(objective, rows):
    return LinearProgram(
        objective=tuple(F(value) for value in objective),
        constraints=tuple(
            Constraint(tuple(F(c) for c in coefficients), sense, F(rhs))
            for coefficients, sense, rhs in rows
        ),
    )


class TestValidation:
    def test_a_program_needs_a_variable(self):
        with pytest.raises(ValidationError):
            LinearProgram(objective=(), constraints=())

    def test_a_row_has_to_match_the_objective_width(self):
        with pytest.raises(ValidationError) as caught:
            LinearProgram(
                objective=(F(1), F(1)),
                constraints=(Constraint((F(1),), Sense.LE, F(1)),),
            )
        assert caught.value.detail["expected"] == 2


class TestUnconstrained:
    def test_all_costs_positive_settles_at_the_origin(self):
        found = solve(program([1, 2], []))
        assert found.status is Status.OPTIMAL
        assert found.values == (F(0), F(0))
        assert found.objective_value == 0

    def test_a_negative_cost_with_nothing_holding_it_is_unbounded(self):
        assert solve(program([-1], [])).status is Status.UNBOUNDED


class TestSolving:
    def test_a_textbook_maximisation(self):
        found = solve(
            program(
                [-3, -5],
                [
                    ([1, 0], Sense.LE, 4),
                    ([0, 2], Sense.LE, 12),
                    ([3, 2], Sense.LE, 18),
                ],
            )
        )
        assert found.is_optimal
        assert found.values == (F(2), F(6))
        assert found.objective_value == -36

    def test_equalities_are_satisfied_exactly(self):
        found = solve(
            program([1, 1], [([1, 1], Sense.EQ, 10), ([1, -1], Sense.EQ, 2)])
        )
        assert found.values == (F(6), F(4))

    def test_a_greater_than_row_is_honoured(self):
        found = solve(program([1], [([1], Sense.GE, 5)]))
        assert found.values == (F(5),)
        assert found.objective_value == 5

    def test_a_negative_right_hand_side_is_flipped_not_refused(self):
        found = solve(program([1], [([-1], Sense.LE, -5)]))
        assert found.values == (F(5),)

    def test_contradictory_rows_are_infeasible(self):
        found = solve(program([1], [([1], Sense.GE, 5), ([1], Sense.LE, 2)]))
        assert found.status is Status.INFEASIBLE
        assert found.values == ()

    def test_an_open_direction_is_unbounded(self):
        found = solve(program([-1, 0], [([0, 1], Sense.LE, 3)]))
        assert found.status is Status.UNBOUNDED

    def test_the_answers_are_exact_rationals(self):
        found = solve(program([-1], [([3], Sense.LE, 1)]))
        assert found.values == (F(1, 3),)
        assert found.objective_value == F(-1, 3)


class TestDegeneracy:
    def test_identical_columns_do_not_send_it_round_forever(self):
        found = solve(
            program(
                [-1, -1, -1, -1],
                [
                    ([1, 1, 1, 1], Sense.LE, 10),
                    ([1, 1, 1, 1], Sense.LE, 10),
                    ([1, 1, 1, 1], Sense.LE, 10),
                ],
            )
        )
        assert found.is_optimal
        assert sum(found.values) == 10

    def test_a_degenerate_vertex_still_terminates(self):
        found = solve(
            program(
                [-1, -1],
                [
                    ([1, 0], Sense.LE, 0),
                    ([0, 1], Sense.LE, 5),
                    ([1, 1], Sense.LE, 5),
                ],
            )
        )
        assert found.is_optimal
        assert found.values == (F(0), F(5))

    def test_the_klee_minty_shape_finishes(self):
        found = solve(
            program(
                [-4, -2, -1],
                [
                    ([1, 0, 0], Sense.LE, 5),
                    ([4, 1, 0], Sense.LE, 25),
                    ([8, 4, 1], Sense.LE, 125),
                ],
            )
        )
        assert found.is_optimal
        assert found.objective_value == -125

    def test_the_iteration_limit_is_enforced(self):
        with pytest.raises(ValidationError):
            solve(
                program(
                    [-3, -5],
                    [([1, 0], Sense.LE, 4), ([0, 2], Sense.LE, 12), ([3, 2], Sense.LE, 18)],
                ),
                iteration_limit=1,
            )


class TestReporting:
    def test_an_optimal_solution_says_so(self):
        assert solve(program([1], [([1], Sense.GE, 1)])).is_optimal

    def test_an_infeasible_solution_is_not_optimal(self):
        assert not Solution(status=Status.INFEASIBLE).is_optimal

    def test_the_iteration_count_comes_back(self):
        found = solve(
            program(
                [-3, -5],
                [([1, 0], Sense.LE, 4), ([0, 2], Sense.LE, 12), ([3, 2], Sense.LE, 18)],
            )
        )
        assert found.iterations >= 2


def test_a_mixed_sense_program_with_all_three_kinds_of_row():
    found = solve(
        program(
            [2, 3, 1],
            [
                ([1, 1, 1], Sense.EQ, 100),
                ([1, 0, 0], Sense.GE, 20),
                ([0, 1, 0], Sense.LE, 30),
            ],
        )
    )
    assert found.is_optimal
    assert sum(found.values) == 100
    assert found.values[0] >= 20
    assert found.values[1] <= 30
