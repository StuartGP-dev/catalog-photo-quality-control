from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Sequence

from .models import Recipe
from .recipe_schema import ParameterSpec, RecipeSchema


@dataclass(frozen=True, slots=True)
class RecipeProposal:
    recipe: Recipe
    source: str


class RecipeGenerator:
    def __init__(
        self,
        schema: RecipeSchema,
        allocation: dict[str, float],
        *,
        seed: int | None = None,
        mutation_fraction: float = 0.12,
    ):
        self.schema = schema
        self.allocation = allocation
        self.random = random.Random(seed)
        self.mutation_fraction = mutation_fraction

    def _sample_value(self, spec: ParameterSpec):
        if spec.kind == "choice":
            return self.random.choice(spec.choices)
        assert spec.minimum is not None and spec.maximum is not None
        if spec.kind == "int" and spec.distribution == "choice":
            candidates = list(range(int(spec.minimum), int(spec.maximum) + 1, 2))
            if int(spec.default) not in candidates:
                candidates.append(int(spec.default))
            return self.random.choice(candidates)
        if spec.distribution == "triangular":
            value = self.random.triangular(spec.minimum, spec.maximum, spec.default)
        else:
            value = self.random.uniform(spec.minimum, spec.maximum)
        return round(value) if spec.kind == "int" else value

    def random_recipe(self) -> Recipe:
        for _ in range(100):
            values = {}
            for name, spec in self.schema.parameters.items():
                active = spec.enabled and self.random.random() < spec.activation_probability
                values[name] = self._sample_value(spec) if active else spec.default
            try:
                return self.schema.canonicalize(values)
            except ValueError:
                continue
        return self.schema.canonicalize({})

    def mutate(self, parent: Recipe) -> Recipe:
        for _ in range(100):
            values = dict(parent.parameters)
            enabled = [spec for spec in self.schema.parameters.values() if spec.enabled]
            count = max(1, min(4, round(len(enabled) * self.mutation_fraction)))
            for spec in self.random.sample(enabled, count):
                if spec.kind == "choice":
                    values[spec.name] = self.random.choice(spec.choices)
                    continue
                assert spec.minimum is not None and spec.maximum is not None
                span = spec.maximum - spec.minimum
                proposed = float(values[spec.name]) + self.random.gauss(0, span * self.mutation_fraction)
                proposed = min(spec.maximum, max(spec.minimum, proposed))
                values[spec.name] = round(proposed) if spec.kind == "int" else proposed
            try:
                mutated = self.schema.canonicalize(values)
                if mutated.recipe_hash != parent.recipe_hash:
                    return mutated
            except ValueError:
                continue
        return self.random_recipe()

    def propose(self, proven: Sequence[Recipe]) -> RecipeProposal:
        draw = self.random.random()
        random_limit = self.allocation["random"]
        proven_limit = random_limit + self.allocation["proven"]
        if draw < random_limit or not proven:
            return RecipeProposal(self.random_recipe(), "random")
        parent = self.random.choice(list(proven))
        if draw < proven_limit:
            return RecipeProposal(parent, "proven")
        return RecipeProposal(self.mutate(parent), "mutation")
