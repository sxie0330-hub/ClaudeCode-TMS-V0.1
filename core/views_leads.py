"""Inquiries and quotes (询报价): Lead list / detail, scope, carrier RFQ, quotes, conversion to a project."""
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.db.models import Q
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from . import ai, ai_leads, oog, quote_pdf, quotes
from .forms import (CarrierQuoteForm, CustomerQuoteForm, LeadCargoForm, LeadForm, LeadLaneForm, carrier_quote_lines_formset,
                    customer_quote_lines_formset)
from .models import Carrier, CarrierQuote, Charge, CustomerQuote, Lead, LeadCargo, LeadLane, Profile, Role, TrailerType
from .notify import notify
from .permissions import can_quote
from .views import _confirm_delete, _edit, _need


def _lead(request, pk):
    _need(can_quote(request.user))
    return get_object_or_404(Lead, pk=pk)


def lead_list(request):
    _need(can_quote(request.user))
    qs = Lead.objects.select_related("customer", "owner", "project")
    st = request.GET.get("status", "open")
    if st == "open":
        qs = qs.filter(status__in=Lead.OPEN)
    elif st in Lead.Status.values:
        qs = qs.filter(status=st)
    if request.GET.get("mine"):
        qs = qs.filter(owner=request.user)
    q = request.GET.get("q", "").strip()
    if q:
        qs = qs.filter(Q(number__icontains=q) | Q(title__icontains=q) | Q(prospect_name__icontains=q) | Q(customer__name__icontains=q)
                       | Q(customer_ref__icontains=q))
    return render(request, "core/lead_list.html", {"leads": qs[:300], "status": st, "statuses": Lead.Status.choices, "q": q,
                                                   "mine": request.GET.get("mine"), "today": timezone.localdate()})


def _docs(request):
    docs, text_parts = [], []
    for f in request.FILES.getlist("files"):
        mime = ai.doc_mime(f.name)
        if mime:
            if f.size > ai.MAX_DOC_BYTES:
                raise ai.AiError(f"「{f.name}」太大（超过 {ai.MAX_DOC_BYTES // 1024 // 1024}MB）")
            docs.append({"name": f.name, "mime": mime, "data": f.read()})
        else:
            text_parts.append(f"=== 附带表格：{f.name} ===\n" + ai.sheet_to_text(f))
    return docs, text_parts


def lead_new(request):
    _need(can_quote(request.user))
    text = request.POST.get("text", "")
    subject = request.POST.get("subject", "").strip()
    conn = getattr(request.user, "gmail", None)
    if request.method == "POST":
        try:
            docs, sheets = _docs(request)
            mail = ""
            if subject:  # read the original thread from the person's own mailbox: sender, replies, attachments
                from . import mailscan

                if conn is None:
                    raise ai.AiError("要按邮件主题读取原始邮件，请先在「邮箱」页连接你自己的 Gmail；也可以清空主题，直接粘贴邮件内容")
                try:
                    token, msgs = mailscan.find_messages(conn, subject)
                except mailscan.ScanError as e:
                    raise ai.AiError(str(e)) from e
                mail = mailscan._messages_text(msgs)
                docs = mailscan.fetch_docs(token, msgs) + docs
            src = request.POST.get("source") if request.POST.get("source") in Lead.Source.values else Lead.Source.EMAIL
            lead, problems = ai_leads.create_lead("\n\n".join(x for x in [mail, text] + sheets if x.strip()).strip(),
                                                  request.user, docs=docs, source=src)
        except ai.AiError as e:
            messages.error(request, str(e))
        else:
            messages.success(request, f"已建立询价 {lead.number}，请核对 AI 整理的内容")
            for p in problems:
                messages.warning(request, p)
            return redirect(lead)
    return render(request, "core/lead_new.html", {"text": text, "subject": subject, "enabled": ai.enabled(), "sources": Lead.Source.choices,
                                                  "max_chars": ai.MAX_EMAIL_CHARS, "gmail": conn})


def lead_create(request):
    _need(can_quote(request.user))
    lead = Lead(owner=request.user, created_by=request.user)
    resp = _edit(request, LeadForm, lead, "手动新建询价", lambda o: o.get_absolute_url(), cancel_url="/leads/")
    if request.method == "POST" and lead.pk and lead.owner_id:
        notify(lead.owner, f"{request.user.get_full_name() or request.user.email} 新建询价 {lead.number} {lead.title}，交给你负责",
               lead.get_absolute_url(), exclude=request.user)
    return resp


def lead_edit(request, pk):
    lead = _lead(request, pk)
    before = lead.owner_id
    resp = _edit(request, LeadForm, lead, f"编辑询价 {lead.number}", lead.get_absolute_url())
    lead.refresh_from_db()
    if request.method == "POST" and lead.owner_id and lead.owner_id != before:
        notify(lead.owner, f"{request.user.get_full_name() or request.user.email} 把询价 {lead.number} {lead.title} 交给你负责",
               lead.get_absolute_url(), exclude=request.user)
    return resp


def lead_detail(request, pk):
    lead = _lead(request, pk)
    lanes = list(lead.lanes.all())
    cargo = list(lead.cargo.select_related("lane"))
    trailers = list(TrailerType.objects.filter(active=True))
    screens = [oog.screen_lane(lane, oog.lane_cargo(lane, lanes, cargo), trailers) for lane in lanes]
    cqs = list(lead.carrier_quotes.select_related("carrier", "lane").prefetch_related("lines"))
    chosen = {q.pk for q in quotes.chosen_quotes(lead).values()}
    by_lane = []
    for lane in lanes + [None]:
        rows = [q for q in cqs if q.lane_id == (lane.pk if lane else None)]
        if rows or lane is not None:
            by_lane.append((lane, sorted(rows, key=lambda q: (q.status == CarrierQuote.Status.DECLINED, q.total or Decimal("1e12")))))
    customer_quotes = list(lead.customer_quotes.prefetch_related("lines"))
    for cq in customer_quotes:
        cq.m = quotes.margin(cq)
    return render(request, "core/lead_detail.html", {
        "lead": lead, "lanes": lanes, "cargo": cargo, "screens": screens, "by_lane": by_lane, "chosen": chosen,
        "customer_quotes": customer_quotes, "questions": [x for x in lead.questions.splitlines() if x.strip()],
    })


@require_POST
def lead_add_customer(request, pk):
    """New customer from the inquiry, in one click (or link to an existing one with the same name)."""
    from .models import Customer

    lead = _lead(request, pk)
    name = (lead.prospect_name or "").strip()
    if lead.customer_id or not name:
        return redirect(lead)
    c = Customer.objects.filter(name__iexact=name).first()
    if c is None:
        c = Customer.objects.create(name=name[:120], contact_name=lead.contact_name[:120], contact_phone=lead.contact_phone[:60],
                                    contact_email=lead.contact_email if "@" in lead.contact_email and len(lead.contact_email) <= 254 else "")
        messages.success(request, f"已加入客户库：{c.name}")
    else:
        messages.success(request, f"客户库里已有「{c.name}」，已关联")
    lead.customer, lead.prospect_name = c, ""
    lead.save(update_fields=["customer", "prospect_name", "updated_at"])
    return redirect(lead)


# --- scope rows ---------------------------------------------------------------
def lane_edit(request, pk, lid=None):
    lead = _lead(request, pk)
    lane = get_object_or_404(LeadLane, pk=lid, lead=lead) if lid else LeadLane(lead=lead, sequence=lead.lanes.count() + 1)
    return _edit(request, LeadLaneForm, lane, "运输段", lead.get_absolute_url())


def cargo_edit(request, pk, cid=None):
    lead = _lead(request, pk)
    cargo = get_object_or_404(LeadCargo, pk=cid, lead=lead) if cid else LeadCargo(lead=lead)
    if not cid and lead.lanes.count() == 1:
        cargo.lane = lead.lanes.first()
    return _edit(request, LeadCargoForm, cargo, "货物", lead.get_absolute_url(), form_kwargs={"lead": lead})


def row_delete(request, pk, kind, rid):
    lead = _lead(request, pk)
    model = {"lane": LeadLane, "cargo": LeadCargo, "carrier": CarrierQuote, "quote": CustomerQuote}[kind]
    obj = get_object_or_404(model, pk=rid, lead=lead)
    return _confirm_delete(request, obj, "删除", lead.get_absolute_url(), lead.get_absolute_url())


# --- carrier RFQ ----------------------------------------------------------------
def lead_rfq(request, pk):
    """Draft RFQ e-mails for carriers. A person sends them; this page only records who was asked."""
    lead = _lead(request, pk)
    lanes = list(lead.lanes.all())
    picked = request.POST.getlist("lanes") or request.GET.getlist("lanes")
    use = [ln for ln in lanes if str(ln.pk) in picked] or lanes
    kind = request.GET.get("kind", "")
    carriers = Carrier.objects.all()
    if kind:
        carriers = carriers.filter(kind=kind)
    if request.method == "POST":
        ids = request.POST.getlist("carriers")
        extra = [x.strip() for x in request.POST.get("extra", "").replace(";", "\n").replace(",", "\n").splitlines() if x.strip()]
        lane_targets = use if len(use) > 1 else (use or [None])
        n = 0
        for c in Carrier.objects.filter(pk__in=ids):
            for ln in lane_targets:
                CarrierQuote.objects.get_or_create(lead=lead, carrier=c, lane=ln, status=CarrierQuote.Status.REQUESTED,
                                                   defaults={"contact_email": c.contact_email, "requested_at": timezone.now()})
                n += 1
        for email in extra:
            for ln in lane_targets:
                CarrierQuote.objects.get_or_create(lead=lead, carrier=None, carrier_name=email, lane=ln, status=CarrierQuote.Status.REQUESTED,
                                                   defaults={"contact_email": email, "requested_at": timezone.now()})
                n += 1
        if lead.status in (Lead.Status.NEW, Lead.Status.SCOPING):
            lead.status = Lead.Status.SOURCING
            lead.save(update_fields=["status", "updated_at"])
        messages.success(request, f"已记录 {n} 条询价（邮件需要你自己发出）")
        return redirect(lead)
    subject, body = quotes.rfq_email(lead, request.user, use)
    rows = []
    for c in carriers[:300]:
        s, b = quotes.rfq_email(lead, request.user, use, contact_name=(c.contact_name or "").split(" ")[0])
        rows.append((c, quotes.gmail_compose_url(c.contact_email, s, b) if c.contact_email else ""))
    return render(request, "core/lead_rfq.html", {
        "lead": lead, "lanes": lanes, "use": use, "subject": subject, "body": body,
        "gmail_bcc": quotes.gmail_compose_url("", subject, body), "rows": rows, "kinds": Carrier.Kind.choices, "kind": kind,
    })


def carrier_quote_edit(request, pk, qid=None):
    lead = _lead(request, pk)
    q = get_object_or_404(CarrierQuote, pk=qid, lead=lead) if qid else CarrierQuote(lead=lead, status=CarrierQuote.Status.QUOTED)
    Lines = carrier_quote_lines_formset()
    if request.method == "POST":
        form = CarrierQuoteForm(request.POST, instance=q, lead=lead)
        fs = Lines(request.POST, instance=q)
        if form.is_valid() and fs.is_valid():
            q = form.save()
            fs.instance = q
            fs.save()
            if lead.status in (Lead.Status.NEW, Lead.Status.SCOPING):
                lead.status = Lead.Status.SOURCING
                lead.save(update_fields=["status", "updated_at"])
            messages.success(request, "已保存")
            return redirect(lead)
    else:
        form, fs = CarrierQuoteForm(instance=q, lead=lead), Lines(instance=q)
    return render(request, "core/lead_formset.html", {"title": f"承运商报价 · {lead.number}", "form": form, "fs": fs,
                                                      "cancel_url": lead.get_absolute_url(), "kind": "carrier"})


@require_POST
def carrier_quote_select(request, pk, qid):
    lead = _lead(request, pk)
    q = get_object_or_404(CarrierQuote, pk=qid, lead=lead)
    lead.carrier_quotes.filter(lane=q.lane, status=CarrierQuote.Status.SELECTED).update(status=CarrierQuote.Status.QUOTED)
    q.status = CarrierQuote.Status.SELECTED
    q.save(update_fields=["status", "updated_at"])
    messages.success(request, f"已选用 {q.carrier_label}")
    return redirect(lead)


def lead_reply(request, pk):
    """Paste a carrier's reply (or upload its PDF) -> AI drafts the quotes -> a person checks and saves."""
    lead = _lead(request, pk)
    if request.method == "POST" and request.POST.get("action") == "save":
        try:
            saved = ai_leads.save_reply_quotes(lead, request.POST, request.user)
        except ai.AiError as e:
            messages.error(request, str(e))
            return redirect("lead_reply", pk=lead.pk)
        messages.success(request, f"已保存 {len(saved)} 家承运商报价")
        return redirect(lead)
    draft = None
    text = request.POST.get("text", "")
    if request.method == "POST":
        try:
            docs, sheets = _docs(request)
            draft = ai_leads.read_reply(lead, "\n\n".join([text] + sheets).strip(), docs=docs, user=request.user)
        except ai.AiError as e:
            messages.error(request, str(e))
    return render(request, "core/lead_reply.html", {
        "lead": lead, "draft": draft, "text": text, "enabled": ai.enabled(), "lanes": list(lead.lanes.all()),
        "carriers": Carrier.objects.all()[:500], "types": Charge.Type.choices,
        "statuses": CarrierQuote.Status.choices,
    })


# --- customer quotes ----------------------------------------------------------------
@require_POST
def customer_quote_build(request, pk):
    lead = _lead(request, pk)
    try:
        markup = Decimal(request.POST.get("markup") or "15")
    except InvalidOperation:
        markup = Decimal("15")
    q = quotes.build_customer_quote(lead, markup)
    messages.success(request, f"已生成报价 {q.number}（按成本加价 {markup}%），请核对后再发给客户")
    return redirect("customer_quote_edit", pk=lead.pk, qid=q.pk)


def customer_quote_edit(request, pk, qid):
    lead = _lead(request, pk)
    q = get_object_or_404(CustomerQuote, pk=qid, lead=lead)
    Lines = customer_quote_lines_formset()
    if request.method == "POST":
        form = CustomerQuoteForm(request.POST, instance=q)
        fs = Lines(request.POST, instance=q)
        for f in fs.forms:
            f.fields["lane"].queryset = lead.lanes.all()
        if form.is_valid() and fs.is_valid():
            q = form.save()
            fs.save()
            if q.status == CustomerQuote.Status.SENT and lead.status in (Lead.Status.NEW, Lead.Status.SCOPING, Lead.Status.SOURCING):
                lead.status = Lead.Status.QUOTED
                lead.save(update_fields=["status", "updated_at"])
            messages.success(request, "已保存")
            return redirect("customer_quote_edit", pk=lead.pk, qid=q.pk)
    else:
        form, fs = CustomerQuoteForm(instance=q), Lines(instance=q)
        for f in fs.forms:
            f.fields["lane"].queryset = lead.lanes.all()
    subject, body = quotes.customer_email(q, request.user)
    return render(request, "core/lead_formset.html", {
        "title": f"客户报价 {q.number}", "form": form, "fs": fs, "cancel_url": lead.get_absolute_url(), "kind": "customer",
        "quote": q, "lead": lead, "m": quotes.margin(q), "subject": subject, "body": body,
        "gmail": quotes.gmail_compose_url(lead.contact_email if "@" in lead.contact_email else "", subject, body),
    })


def customer_quote_pdf(request, pk, qid):
    lead = _lead(request, pk)
    q = get_object_or_404(CustomerQuote, pk=qid, lead=lead)
    resp = HttpResponse(quote_pdf.render(q), content_type="application/pdf")
    resp["Content-Disposition"] = f'inline; filename="Quotation-{q.number}.pdf"'
    return resp


# --- outcome ----------------------------------------------------------------------
@require_POST
def lead_status(request, pk):
    lead = _lead(request, pk)
    action = request.POST.get("action")
    if action == "lost":
        lead.status, lead.lost_reason = Lead.Status.LOST, request.POST.get("reason", "")[:255]
    elif action == "cancel":
        lead.status = Lead.Status.CANCELLED
    elif action == "reopen" and not lead.project_id:
        lead.status = Lead.Status.SCOPING
    elif action == "quoted":
        lead.status = Lead.Status.QUOTED
    lead.save()
    messages.success(request, f"状态：{lead.get_status_display()}")
    return redirect(lead)


def lead_convert(request, pk):
    lead = _lead(request, pk)
    if lead.project_id:
        messages.info(request, "已经转成项目了")
        return redirect(lead.project)
    lanes = list(lead.lanes.all())
    cqs = list(lead.carrier_quotes.select_related("carrier").prefetch_related("lines").exclude(status=CarrierQuote.Status.DECLINED))
    chosen = quotes.chosen_quotes(lead)
    customer_quotes = list(lead.customer_quotes.exclude(status=CustomerQuote.Status.REJECTED))
    ops = get_user_model().objects.filter(Profile.with_roles(Role.OP, Role.MANAGEMENT), is_active=True).distinct().order_by("first_name", "email")
    if request.method == "POST":
        owner = ops.filter(pk=request.POST.get("owner") or 0).first()
        picks = {}
        for lane in lanes:
            qid = request.POST.get(f"lane_{lane.pk}")
            picks[lane.pk] = next((q for q in cqs if str(q.pk) == qid), None)
        if len(lanes) == 1 and picks.get(lanes[0].pk) is None and request.POST.get("lane_none"):
            picks[None] = next((q for q in cqs if str(q.pk) == request.POST.get("lane_none")), None)
        cust_q = next((q for q in customer_quotes if str(q.pk) == request.POST.get("customer_quote")), None)
        try:
            project = quotes.convert(lead, request.user, owner=owner, project_name=request.POST.get("name", "").strip(),
                                     carrier_quotes=picks, customer_quote=cust_q)
        except ValueError as e:
            messages.error(request, str(e))
        else:
            notify(owner, f"询价 {lead.number} 成单：项目 {project.number} {project.name} 交给你负责（{project.shipments.count()} 个 Load）",
                   project.get_absolute_url(), exclude=request.user)
            messages.success(request, f"已成单：建立项目 {project.number} 和 {project.shipments.count()} 个 Load")
            return redirect(project)
    default_cq = next((q for q in customer_quotes if q.status == CustomerQuote.Status.ACCEPTED), None) or (customer_quotes[0] if customer_quotes else None)
    lane_rows = [(lane, [q for q in cqs if q.lane_id in (lane.pk, None)], (chosen.get(lane.pk) or chosen.get(None) if len(lanes) == 1 else chosen.get(lane.pk)))
                 for lane in lanes]
    return render(request, "core/lead_convert.html", {
        "lead": lead, "lane_rows": lane_rows, "customer_quotes": customer_quotes, "default_cq": default_cq, "ops": ops,
        "default_owner": lead.owner if lead.owner and ops.filter(pk=lead.owner_id).exists() else None,
        "trucks": sum(ln.trucks for ln in lanes),
    })
