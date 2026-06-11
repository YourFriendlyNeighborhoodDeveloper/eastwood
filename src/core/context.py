from dataclasses import dataclass
from .settings import Settings, load_settings
from ..pipeline.topics import Topics
from ..pipeline.producer import Producer
from ..storage.postgres import PostgresStore
from ..storage.redis import RedisStore
from ..storage.repository.leads_repo import LeadsRepo
from ..storage.repository.consent_repo import ConsentRepo
from ..storage.repository.suppression_repo import SuppressionRepo
from ..storage.repository.booking_repo import BookingRepo
from ..storage.repository.appointments_repo import AppointmentsRepo
from ..notify.sms import SmsClient

@dataclass(frozen=True)
class AppContext:
    settings: Settings
    topics: Topics
    producer: Producer
    pg: PostgresStore
    redis: RedisStore
    leads: LeadsRepo
    consent: ConsentRepo
    suppression: SuppressionRepo
    booking: BookingRepo
    appointments: AppointmentsRepo
    sms: SmsClient

def build_context() -> AppContext:
    s = load_settings()
    topics = Topics.from_dict(s.topics)
    producer = Producer(bootstrap_servers=s.kafka_bootstrap, client_id=s.kafka_client_id)
    pg = PostgresStore.from_cfg(s.postgres)
    redis = RedisStore.from_cfg(s.redis)
    leads = LeadsRepo(pg)
    consent = ConsentRepo(pg)
    suppression = SuppressionRepo(pg, redis)
    booking = BookingRepo(pg)
    appointments = AppointmentsRepo(pg)
    sms = SmsClient(s)
    return AppContext(
        settings=s,
        topics=topics,
        producer=producer,
        pg=pg,
        redis=redis,
        leads=leads,
        consent=consent,
        suppression=suppression,
        booking=booking,
        appointments=appointments,
        sms=sms,
    )