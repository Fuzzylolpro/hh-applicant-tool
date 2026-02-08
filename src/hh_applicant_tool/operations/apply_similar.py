from __future__ import annotations

import argparse
import logging
import random
from pathlib import Path
from typing import TYPE_CHECKING, Iterator

from ..ai.base import AIError
from ..api import BadResponse, Redirect, datatypes
from ..api.datatypes import PaginatedItems, SearchVacancy
from ..api.errors import ApiError, LimitExceeded
from ..main import BaseNamespace, BaseOperation
from ..storage.repositories.errors import RepositoryError
from ..utils.string import (
    bool2str,
    rand_text,
    shorten,
    unescape_string,
)

if TYPE_CHECKING:
    from ..main import HHApplicantTool


logger = logging.getLogger(__package__)


class Namespace(BaseNamespace):
    resume_id: str | None
    message_list_path: Path
    ignore_employers: Path | None
    force_message: bool
    use_ai: bool
    first_prompt: str
    prompt: str
    order_by: str
    search: str
    schedule: str
    dry_run: bool
    experience: str
    employment: list[str] | None
    area: list[str] | None
    metro: list[str] | None
    professional_role: list[str] | None
    industry: list[str] | None
    employer_id: list[str] | None
    excluded_employer_id: list[str] | None
    currency: str | None
    salary: int | None
    only_with_salary: bool
    label: list[str] | None
    period: int | None
    date_from: str | None
    date_to: str | None
    top_lat: float | None
    bottom_lat: float | None
    left_lng: float | None
    right_lng: float | None
    sort_point_lat: float | None
    sort_point_lng: float | None
    no_magic: bool
    premium: bool
    per_page: int
    total_pages: int
    excluded_terms: str | None


class Operation(BaseOperation):
    """Откликнуться на все подходящие вакансии."""

    __aliases__ = ("apply",)

    def run(self, tool: HHApplicantTool) -> None:
        self.tool = tool
        self.api_client = tool.api_client
        args: Namespace = tool.args

        self.application_messages = self._get_application_messages(
            args.message_list_path
        )

        self.resume_id = args.resume_id
        self.search = args.search
        self.total_pages = args.total_pages
        self.per_page = args.per_page
        self.dry_run = args.dry_run
        self.force_message = args.force_message
        self.openai_chat = (
            tool.get_openai_chat(args.first_prompt) if args.use_ai else None
        )
        self.pre_prompt = args.prompt
        self.excluded_terms = self._parse_excluded_terms(args.excluded_terms)

        self._apply_similar()

    def _apply_similar(self) -> None:
        resumes = self.tool.get_resumes()
        resumes = [
            r for r in resumes
            if r["status"]["id"] == "published"
               and (not self.resume_id or r["id"] == self.resume_id)
        ]

        if not resumes:
            logger.warning("У вас нет опубликованных резюме")
            return

        user = self.tool.get_me()
        seen_employers: set[str] = set()

        for resume in resumes:
            self._apply_resume(resume, user, seen_employers)

        logger.info("📝 Отклики на вакансии разосланы!")

    def _apply_resume(
            self,
            resume: datatypes.Resume,
            user: datatypes.User,
            seen_employers: set[str],
    ) -> None:
        logger.info(
            "🚀 Начинаю рассылку откликов для резюме: %s (%s)",
            resume["alternate_url"],
            resume["title"],
        )

        placeholders = {
            "first_name": user.get("first_name") or "",
            "last_name": user.get("last_name") or "",
            "email": user.get("email") or "",
            "phone": user.get("phone") or "",
            "resume_title": resume.get("title") or "",
        }

        do_apply = True

        for vacancy in self._get_similar_vacancies(resume["id"]):
            try:
                if not do_apply:
                    continue

                if vacancy.get("archived") or vacancy.get("has_test"):
                    continue

                relations = vacancy.get("relations", [])
                if relations:
                    if "got_rejection" in relations:
                        logger.warning(
                            "⛔ Пришел отказ от %s",
                            vacancy["alternate_url"],
                        )
                    continue

                if self._is_excluded(vacancy):
                    logger.warning(
                        "Вакансия содержит недопустимые слова: %s",
                        vacancy["alternate_url"],
                    )
                    continue

                params = {
                    "resume_id": resume["id"],
                    "vacancy_id": vacancy["id"],
                    "message": "",
                }

                if self.force_message or vacancy.get("response_letter_required"):
                    if self.openai_chat:
                        msg = self.openai_chat.send_message(
                            f"{self.pre_prompt}\n{vacancy.get('name')}"
                        )
                    else:
                        msg = unescape_string(
                            rand_text(random.choice(self.application_messages))
                            % placeholders
                        )
                    params["message"] = msg

                if not self.dry_run:
                    self.api_client.post(
                        "/negotiations",
                        params,
                        delay=random.uniform(1, 3),
                    )

                logger.info(
                    "📨 Отправили отклик для резюме %s на вакансию %s (%s)",
                    resume["alternate_url"],
                    vacancy["alternate_url"],
                    shorten(vacancy["name"]),
                )

            except LimitExceeded:
                logger.warning(
                    "⚠️ Достигли лимита рассылки для резюме %s",
                    resume["alternate_url"],
                )
                do_apply = False
            except (ApiError, BadResponse, AIError) as ex:
                logger.error(ex)

        logger.info(
            "✅️ Закончили рассылку откликов для резюме: %s (%s)",
            resume["alternate_url"],
            resume["title"],
        )

    def _get_similar_vacancies(self, resume_id: str) -> Iterator[SearchVacancy]:
        for page in range(self.total_pages):
            res: PaginatedItems[SearchVacancy] = self.api_client.get(
                f"/resumes/{resume_id}/similar_vacancies",
                {"page": page, "per_page": self.per_page},
            )
            yield from res.get("items", [])

    @staticmethod
    def _parse_excluded_terms(excluded_terms: str | None) -> list[str]:
        if not excluded_terms:
            return []
        return [x.strip().lower() for x in excluded_terms.split(",") if x.strip()]

    def _is_excluded(self, vacancy: SearchVacancy) -> bool:
        snippet = vacancy.get("snippet") or {}
        text = " ".join(
            [
                vacancy.get("name") or "",
                snippet.get("requirement") or "",
                snippet.get("responsibility") or "",
                ]
        ).lower()
        return any(term in text for term in self.excluded_terms)

    def _get_application_messages(self, path: Path | None) -> list[str]:
        if not path:
            return [
                "Здравствуйте, меня зовут %(first_name)s. Меня заинтересовала вакансия «%(vacancy_name)s».",
                "Прошу рассмотреть мою кандидатуру на вакансию «%(vacancy_name)s».",
            ]
        return [line.strip() for line in path.open(encoding="utf-8") if line.strip()]
